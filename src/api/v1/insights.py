"""Endpoint /v1/insights : agrege les insights proactifs de toutes les sources.

Combine :
- Locations : "loin de chez toi", anniversaires, evolution annee
- Calendar  : evenements imminents (48h)
- Tasks     : overdue + completion rate
- Finance   : grosses transactions inhabituelles, abonnements suspects
- Emails    : non-lus prioritaires, top sender activite

Pas de cron / push ici - c'est un endpoint pull. Le push (ntfy) sera fait
par hub-ingest qui appellera cet endpoint et filtrera par severite.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

logger = logging.getLogger(__name__)

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    CalendarEvent,
    CreditCardTransaction,
    Email,
    HealthMetric,
    LocationVisit,
    Task,
    Transaction,
)
from src.db.session import get_db

router = APIRouter(prefix="/insights", tags=["insights"])


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------


Severity = Literal["critical", "warning", "info", "positive"]


class Insight(BaseModel):
    severity: Severity
    icon: str  # nom Lucide React (ex: "AlertTriangle", "Bell")
    title: str
    description: str
    delta: str | None = None
    action: str | None = None
    action_url: str | None = None
    source: str  # 'locations' | 'calendar' | 'tasks' | 'finance' | 'emails' | 'health'
    metric_value: float | None = None  # pour tri / filtrage
    generated_at: datetime


class InsightsResponse(BaseModel):
    insights: list[Insight]
    generated_at: datetime
    total: int
    by_severity: dict[str, int]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _aware(dt: datetime) -> datetime:
    """Force un datetime en aware UTC.

    SQLite renvoie des datetimes naive ; PostgreSQL des aware. Pour comparer
    avec datetime.now(UTC), on doit toujours passer par cette helper.
    """
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------
# Detectors par source
# ---------------------------------------------------------------------


async def _calendar_insights(db: AsyncSession, now: datetime) -> list[Insight]:
    """Evenements dans les 48h."""
    out: list[Insight] = []
    end = now + timedelta(hours=48)
    q = (
        select(CalendarEvent)
        .where(CalendarEvent.start_at >= now)
        .where(CalendarEvent.start_at <= end)
        .order_by(CalendarEvent.start_at)
        .limit(5)
    )
    rows = (await db.execute(q)).scalars().all()
    for ev in rows:
        start_aw = _aware(ev.start_at)
        delta_h = (start_aw - now).total_seconds() / 3600
        delta = f"dans {int(delta_h)}h" if delta_h >= 1 else f"dans {int(delta_h * 60)} min"
        out.append(
            Insight(
                severity="info" if delta_h > 12 else "warning",
                icon="Calendar",
                title=ev.summary or "Evenement sans titre",
                description=(
                    f"{start_aw.strftime('%d/%m %H:%M')} ({ev.location or 'lieu non precise'})"
                ),
                delta=delta,
                action="Voir l'evenement",
                action_url=f"/calendar?id={ev.id}",
                source="calendar",
                metric_value=delta_h,
                generated_at=now,
            )
        )
    return out


async def _tasks_insights(db: AsyncSession, now: datetime) -> list[Insight]:
    """Tasks pending + overdue."""
    out: list[Insight] = []

    # Overdue (due_at < now ET pas completed)
    q_overdue = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.is_completed.is_(False),
            Task.due_at.is_not(None),
            Task.due_at < now,
        )
    )
    overdue = (await db.execute(q_overdue)).scalar_one()
    if overdue > 0:
        out.append(
            Insight(
                severity="warning" if overdue < 5 else "critical",
                icon="AlertTriangle",
                title=f"{overdue} tache(s) en retard",
                description="Date d'echeance depassee, marquez-les terminees ou reportez-les.",
                delta=f"{overdue}",
                action="Aller aux taches",
                action_url="/tasks",
                source="tasks",
                metric_value=float(overdue),
                generated_at=now,
            )
        )

    # Pending non-overdue
    q_pending = select(func.count()).select_from(Task).where(Task.is_completed.is_(False))
    pending = (await db.execute(q_pending)).scalar_one()
    if pending > 0 and pending != overdue:
        out.append(
            Insight(
                severity="info",
                icon="ListTodo",
                title=f"{pending - overdue} tache(s) en cours",
                description="Pas encore en retard, prevues pour les jours qui viennent.",
                action="Aller aux taches",
                action_url="/tasks",
                source="tasks",
                metric_value=float(pending - overdue),
                generated_at=now,
            )
        )
    return out


async def _finance_insights(db: AsyncSession, now: datetime) -> list[Insight]:
    """Grosses transactions du mois + abonnements potentiels."""
    out: list[Insight] = []
    today = now.date()
    month_start = today.replace(day=1)

    # Plus grosse transaction debit ce mois
    q = (
        select(Transaction)
        .where(Transaction.transaction_date >= month_start)
        .where(Transaction.debit.is_not(None))
        .order_by(Transaction.debit.desc())
        .limit(1)
    )
    biggest = (await db.execute(q)).scalar_one_or_none()
    if biggest is not None and biggest.debit is not None:
        amount = float(biggest.debit)
        if amount >= 500:
            out.append(
                Insight(
                    severity="info",
                    icon="DollarSign",
                    title=f"Plus grosse depense ce mois : {amount:.0f} $",
                    description=(
                        f"{biggest.transaction_date.isoformat()} - {biggest.description[:80]}"
                    ),
                    delta=f"-{amount:.0f} $",
                    action="Voir la transaction",
                    action_url=f"/finances?date={biggest.transaction_date.isoformat()}",
                    source="finance",
                    metric_value=amount,
                    generated_at=now,
                )
            )

    # Abonnements potentiels : transactions credit_card recurrentes
    # Heuristique : meme description avec amount identique sur 3+ mois consecutifs
    q_cc = (
        select(
            CreditCardTransaction.description,
            CreditCardTransaction.amount,
            func.count().label("n"),
        )
        .where(CreditCardTransaction.amount > 0)
        .where(CreditCardTransaction.transaction_date >= today - timedelta(days=120))
        .group_by(CreditCardTransaction.description, CreditCardTransaction.amount)
        .having(func.count() >= 3)
        .order_by(func.count().desc())
        .limit(5)
    )
    subs = (await db.execute(q_cc)).all()
    if subs:
        total_sub_monthly = sum(float(amt) for _, amt, _ in subs)
        out.append(
            Insight(
                severity="info",
                icon="Repeat",
                title=f"{len(subs)} abonnement(s) potentiel(s) detecte(s)",
                description=", ".join(f"{d[:25]} ({float(a):.0f}$)" for d, a, _ in subs[:3]),
                delta=f"~{total_sub_monthly:.0f} $/mois",
                action="Voir les transactions",
                action_url="/finances",
                source="finance",
                metric_value=total_sub_monthly,
                generated_at=now,
            )
        )
    return out


async def _emails_insights(db: AsyncSession, now: datetime) -> list[Insight]:
    """Emails non-lus + top senders recents."""
    out: list[Insight] = []
    q_unread = select(func.count()).select_from(Email).where(Email.is_unread.is_(True))
    unread = (await db.execute(q_unread)).scalar_one()
    if unread > 0:
        out.append(
            Insight(
                severity="info" if unread < 10 else "warning",
                icon="Mail",
                title=f"{unread} email(s) non-lu(s)",
                description="Boite Gmail synchronisee localement.",
                delta=f"{unread}",
                action="Aller aux emails",
                action_url="/emails",
                source="emails",
                metric_value=float(unread),
                generated_at=now,
            )
        )
    return out


async def _locations_insights(db: AsyncSession, now: datetime) -> list[Insight]:
    """'Loin de chez toi' + anniversaires de visites."""
    out: list[Insight] = []

    # Derniere visite HOME
    q_home = (
        select(LocationVisit)
        .where(LocationVisit.semantic_type.in_(["HOME", "INFERRED_HOME"]))
        .order_by(LocationVisit.start_time.desc())
        .limit(1)
    )
    last_home = (await db.execute(q_home)).scalar_one_or_none()
    if last_home is not None:
        last_home_aw = _aware(last_home.start_time)
        days_away = (now - last_home_aw).days
        if days_away >= 7:
            out.append(
                Insight(
                    severity="warning" if days_away > 30 else "info",
                    icon="Home",
                    title="Loin de chez toi",
                    description=(
                        f"Derniere visite HOME : il y a {days_away} jours "
                        f"({last_home_aw.date().isoformat()})"
                    ),
                    delta=f"{days_away} j",
                    action="Voir la carte",
                    action_url=(
                        f"/locations?date={last_home_aw.date().isoformat()}"
                        f"&lat={float(last_home.lat):.6f}&lng={float(last_home.lng):.6f}"
                    ),
                    source="locations",
                    metric_value=float(days_away),
                    generated_at=now,
                )
            )

    # Anniversaires : visites a la meme date il y a 1, 2, 5, 10 ans
    today = now.date()
    for years_ago in (1, 2, 5, 10):
        target = today.replace(year=today.year - years_ago)
        q_ann = (
            select(LocationVisit)
            .where(func.date(LocationVisit.start_time) == target)
            .order_by(LocationVisit.start_time)
            .limit(1)
        )
        try:
            visit = (await db.execute(q_ann)).scalar_one_or_none()
        except ValueError:
            visit = None  # 29 fevrier sur annee non bissextile
        if visit is not None:
            out.append(
                Insight(
                    severity="info",
                    icon="CalendarClock",
                    title=f"Souvenir : il y a {years_ago} an(s)",
                    description=(
                        f"Le {target.isoformat()} tu etais ici "
                        f"({float(visit.lat):.3f}, {float(visit.lng):.3f})"
                    ),
                    delta=f"{years_ago} an(s)",
                    action="Voir la carte",
                    action_url=(
                        f"/locations?date={target.isoformat()}"
                        f"&lat={float(visit.lat):.6f}&lng={float(visit.lng):.6f}"
                    ),
                    source="locations",
                    metric_value=float(years_ago),
                    generated_at=now,
                )
            )
            break  # un seul anniversaire suffit
    return out


async def _health_insights(db: AsyncSession, now: datetime) -> list[Insight]:
    """Insights derives des donnees Garmin/Google Fit (sleep, stress, fitness, HRV)."""
    out: list[Insight] = []
    today = now.date()
    week_ago = today - timedelta(days=7)
    prev_week = today - timedelta(days=14)
    month_ago = today - timedelta(days=30)

    # Helper : moyenne d'une metric sur N derniers jours
    async def _avg(metric: str, since: date) -> float | None:
        q = (
            select(func.avg(HealthMetric.value))
            .where(HealthMetric.metric == metric)
            .where(HealthMetric.date >= since)
        )
        return (await db.execute(q)).scalar_one_or_none()

    # Helper : derniere valeur connue
    async def _latest(metric: str) -> tuple[date, float] | None:
        q = (
            select(HealthMetric.date, HealthMetric.value)
            .where(HealthMetric.metric == metric)
            .order_by(HealthMetric.date.desc())
            .limit(1)
        )
        r = (await db.execute(q)).first()
        return (r.date, float(r.value)) if r else None

    # ── 1. Sommeil insuffisant : moyenne 7j < 6h (360 min) ─────────────────
    sleep_avg_7d = await _avg("sleep_total_min", week_ago)
    if sleep_avg_7d is not None and sleep_avg_7d > 0:
        h = sleep_avg_7d / 60
        if h < 6:
            out.append(
                Insight(
                    severity="warning",
                    icon="MoonOff",
                    title=f"Sommeil insuffisant : {h:.1f}h en moyenne",
                    description="Moins de 6h/nuit cette semaine. Pense a recuperer.",
                    delta=f"{h:.1f}h",
                    action="Voir health",
                    action_url="/health",
                    source="health",
                    metric_value=float(h),
                    generated_at=now,
                )
            )
        elif h >= 7.5:
            out.append(
                Insight(
                    severity="positive",
                    icon="Moon",
                    title=f"Bon rythme de sommeil : {h:.1f}h",
                    description="Tu dors bien cette semaine, garde le rythme.",
                    delta=f"{h:.1f}h",
                    action="Voir health",
                    action_url="/health",
                    source="health",
                    metric_value=float(h),
                    generated_at=now,
                )
            )

    # ── 2. Stress eleve : avg 7j > 50/100 ──────────────────────────────────
    stress_avg_7d = await _avg("stress_avg", week_ago)
    if stress_avg_7d is not None and stress_avg_7d > 0:
        if stress_avg_7d > 50:
            out.append(
                Insight(
                    severity="warning",
                    icon="Zap",
                    title=f"Stress eleve : {stress_avg_7d:.0f}/100",
                    description=(
                        "Niveau de stress moyen de la semaine au-dessus du seuil "
                        "(50). Prends une pause."
                    ),
                    delta=f"{stress_avg_7d:.0f}",
                    action="Voir health",
                    action_url="/health",
                    source="health",
                    metric_value=float(stress_avg_7d),
                    generated_at=now,
                )
            )

    # ── 3. HRV en baisse : 7j vs 7j precedents ─────────────────────────────
    hrv_7d = await _avg("hrv_avg_ms", week_ago)
    hrv_prev = await _avg("hrv_avg_ms", prev_week)
    if hrv_7d and hrv_prev and hrv_7d > 0 and hrv_prev > 0:
        delta_pct = round((hrv_7d - hrv_prev) / hrv_prev * 100)
        if delta_pct <= -15:
            out.append(
                Insight(
                    severity="warning",
                    icon="HeartPulse",
                    title=f"HRV en baisse de {abs(delta_pct)}%",
                    description=(
                        f"HRV moyen 7j : {hrv_7d:.0f} ms vs {hrv_prev:.0f} ms semaine "
                        f"precedente. Signe de fatigue/stress."
                    ),
                    delta=f"{delta_pct:+d}%",
                    action="Voir health",
                    action_url="/health",
                    source="health",
                    metric_value=float(delta_pct),
                    generated_at=now,
                )
            )

    # ── 4. Pas en chute : 7j vs 7j precedents ──────────────────────────────
    steps_7d = await _avg("steps", week_ago)
    steps_prev = await _avg("steps", prev_week)
    if steps_7d and steps_prev and steps_prev > 0:
        delta_pct = round((steps_7d - steps_prev) / steps_prev * 100)
        if delta_pct <= -25:
            out.append(
                Insight(
                    severity="warning",
                    icon="TrendingDown",
                    title=f"Pas en chute de {abs(delta_pct)}%",
                    description=(
                        f"Moyenne 7j : {steps_7d:.0f} vs {steps_prev:.0f} la semaine "
                        f"precedente. Bouge un peu plus."
                    ),
                    delta=f"{delta_pct:+d}%",
                    action="Voir health",
                    action_url="/health",
                    source="health",
                    metric_value=float(delta_pct),
                    generated_at=now,
                )
            )
        elif delta_pct >= 25:
            out.append(
                Insight(
                    severity="positive",
                    icon="TrendingUp",
                    title=f"Pas en hausse de +{delta_pct}%",
                    description=(
                        f"Moyenne 7j : {steps_7d:.0f} vs {steps_prev:.0f}. Continue comme ca."
                    ),
                    delta=f"+{delta_pct}%",
                    action="Voir health",
                    action_url="/health",
                    source="health",
                    metric_value=float(delta_pct),
                    generated_at=now,
                )
            )

    # ── 5. Recovery time long : derniere mesure > 36h ──────────────────────
    recovery = await _latest("recovery_time_h")
    if recovery is not None and recovery[1] >= 36:
        days_old = (today - recovery[0]).days
        if days_old <= 2:  # mesure recente seulement
            out.append(
                Insight(
                    severity="info",
                    icon="BatteryLow",
                    title=f"Recuperation longue : {recovery[1]:.0f}h",
                    description=(
                        f"Recovery time mesure le {recovery[0].isoformat()}. "
                        "Repose-toi avant ta prochaine grosse seance."
                    ),
                    delta=f"{recovery[1]:.0f}h",
                    action="Voir health",
                    action_url="/health",
                    source="health",
                    metric_value=float(recovery[1]),
                    generated_at=now,
                )
            )

    # ── 6. Fitness age vs best ──────────────────────────────────────────────
    fa_now = await _latest("fitness_age")
    fa_best = await _latest("fitness_age_best")
    if fa_now and fa_best and fa_best[1] > 0:
        diff = fa_now[1] - fa_best[1]
        if diff >= 5:
            out.append(
                Insight(
                    severity="warning",
                    icon="Activity",
                    title=f"Fitness age : {fa_now[1]:.0f} (best : {fa_best[1]:.0f})",
                    description=(
                        f"Ecart de +{diff:.0f} ans vs ta meilleure performance. "
                        "L'entrainement de fond paye."
                    ),
                    delta=f"+{diff:.0f} ans",
                    action="Voir health",
                    action_url="/health",
                    source="health",
                    metric_value=float(diff),
                    generated_at=now,
                )
            )
        elif diff <= 0:
            out.append(
                Insight(
                    severity="positive",
                    icon="Trophy",
                    title=f"Fitness age : {fa_now[1]:.0f} (record !)",
                    description="Tu es au plus haut de ta forme — bravo.",
                    delta=f"{fa_now[1]:.0f} ans",
                    action="Voir health",
                    action_url="/health",
                    source="health",
                    metric_value=float(fa_now[1]),
                    generated_at=now,
                )
            )

    # ── 7. Body battery min : energie au plus bas ─────────────────────────
    bb_min_avg = await _avg("body_battery_min", week_ago)
    if bb_min_avg is not None and bb_min_avg > 0 and bb_min_avg < 20:
        out.append(
            Insight(
                severity="warning",
                icon="Battery",
                title=f"Body Battery min : {bb_min_avg:.0f}/100",
                description=(
                    "Tu finis tes journees a tres basse energie. Sommeil + stress a surveiller."
                ),
                delta=f"{bb_min_avg:.0f}",
                action="Voir health",
                action_url="/health",
                source="health",
                metric_value=float(bb_min_avg),
                generated_at=now,
            )
        )

    # ── 8. Frequence cardiaque au repos en hausse (signe stress/fatigue) ──
    rhr_7d = await _avg("heart_rate_resting", week_ago)
    rhr_30d = await _avg("heart_rate_resting", month_ago)
    if rhr_7d and rhr_30d and rhr_30d > 0:
        diff_bpm = rhr_7d - rhr_30d
        if diff_bpm >= 5:
            out.append(
                Insight(
                    severity="warning",
                    icon="Activity",
                    title=f"FC repos en hausse : +{diff_bpm:.0f} bpm",
                    description=(
                        f"Moyenne 7j : {rhr_7d:.0f} bpm vs {rhr_30d:.0f} sur 30j. "
                        "Souvent signe de fatigue ou maladie qui couve."
                    ),
                    delta=f"+{diff_bpm:.0f} bpm",
                    action="Voir health",
                    action_url="/health",
                    source="health",
                    metric_value=float(diff_bpm),
                    generated_at=now,
                )
            )

    return out


# ---------------------------------------------------------------------
# Cross-source LLM insights (Qwen analyse globale)
# ---------------------------------------------------------------------


async def _gather_cross_source_stats(db: AsyncSession, now: datetime) -> dict[str, Any]:
    """Aggregate 7-14j stats per source en JSON compact pour le prompt LLM.

    But : donner au LLM assez de contexte pour generer des observations
    cross-source pertinentes, sans depasser sa context window.
    """
    from datetime import timedelta as _td

    week_ago = now - _td(days=7)
    two_weeks_ago = now - _td(days=14)

    stats: dict[str, Any] = {"now": now.isoformat()}

    # Health : moyennes 7j vs 7j precedents
    try:
        from src.db.models import HealthMetric

        for metric_name in ("steps", "sleep_seconds", "resting_heart_rate", "stress"):
            q_recent = (
                select(func.avg(HealthMetric.value))
                .where(HealthMetric.metric == metric_name)
                .where(HealthMetric.date >= week_ago.date())
            )
            q_prev = (
                select(func.avg(HealthMetric.value))
                .where(HealthMetric.metric == metric_name)
                .where(HealthMetric.date >= two_weeks_ago.date())
                .where(HealthMetric.date < week_ago.date())
            )
            recent = (await db.execute(q_recent)).scalar()
            prev = (await db.execute(q_prev)).scalar()
            if recent or prev:
                stats[f"health_{metric_name}"] = {
                    "avg_7d": round(float(recent), 1) if recent else None,
                    "avg_prev_7d": round(float(prev), 1) if prev else None,
                }
    except Exception as e:
        logger.debug("insights_stats_health_failed err=%r", e)

    # Finance : depenses 7j vs 7j precedents
    try:
        from src.db.models import Transaction

        q_recent = select(func.sum(Transaction.debit)).where(
            Transaction.transaction_date >= week_ago.date()
        )
        q_prev = (
            select(func.sum(Transaction.debit))
            .where(Transaction.transaction_date >= two_weeks_ago.date())
            .where(Transaction.transaction_date < week_ago.date())
        )
        recent = (await db.execute(q_recent)).scalar()
        prev = (await db.execute(q_prev)).scalar()
        if recent or prev:
            stats["finance_spend"] = {
                "spend_7d_cad": round(float(recent), 2) if recent else 0,
                "spend_prev_7d_cad": round(float(prev), 2) if prev else 0,
            }
    except Exception as e:
        logger.debug("insights_stats_finance_failed err=%r", e)

    # Locations : visites + jours hors-maison
    try:
        from src.db.models import LocationVisit

        q_visits = (
            select(func.count())
            .select_from(LocationVisit)
            .where(LocationVisit.start_at >= week_ago)
        )
        n_visits = (await db.execute(q_visits)).scalar() or 0

        q_last_home = (
            select(LocationVisit.start_at)
            .where(LocationVisit.semantic_type == "HOME")
            .order_by(LocationVisit.start_at.desc())
            .limit(1)
        )
        last_home = (await db.execute(q_last_home)).scalar()
        days_since_home = None
        if last_home:
            days_since_home = (now - _aware(last_home)).days

        stats["locations"] = {
            "visits_7d": int(n_visits),
            "days_since_last_home": days_since_home,
        }
    except Exception as e:
        logger.debug("insights_stats_locations_failed err=%r", e)

    # Browser : top domaines + total visites 7j (utile pour cross-ref)
    try:
        from src.db.models import BrowserHistory

        q_b = (
            select(func.count())
            .select_from(BrowserHistory)
            .where(BrowserHistory.visited_at >= week_ago)
        )
        n_b = (await db.execute(q_b)).scalar() or 0

        q_top = (
            select(BrowserHistory.domain, func.count().label("c"))
            .where(BrowserHistory.visited_at >= week_ago)
            .group_by(BrowserHistory.domain)
            .order_by(func.count().desc())
            .limit(5)
        )
        top = [{"domain": r[0], "count": r[1]} for r in (await db.execute(q_top)).all()]
        if n_b or top:
            stats["browser"] = {"visits_7d": int(n_b), "top_domains": top}
    except Exception as e:
        logger.debug("insights_stats_browser_failed err=%r", e)

    # Tasks : pending vs done
    try:
        from src.db.models import Task

        n_pending = (
            await db.execute(
                select(func.count()).select_from(Task).where(Task.status == "needsAction")
            )
        ).scalar() or 0
        n_done_7d = (
            await db.execute(
                select(func.count())
                .select_from(Task)
                .where(Task.status == "completed")
                .where(Task.completed_at >= week_ago)
            )
        ).scalar() or 0
        stats["tasks"] = {
            "pending": int(n_pending),
            "done_7d": int(n_done_7d),
        }
    except Exception as e:
        logger.debug("insights_stats_tasks_failed err=%r", e)

    return stats


_LLM_INSIGHT_PROMPT = """Tu es l'IA personnelle de Marc, francophone québécois.
Voici ses statistiques agrégées sur 7-14 jours, JSON :

```json
{stats_json}
```

Génère 1 à 3 insights cross-source (qui croisent au moins 2 sources) en français
québécois, factuel, sans flatterie. Format STRICT : un JSON pur (pas de markdown,
pas d'explication), avec un tableau d'objets :

```json
[
  {{"severity": "info|warning|positive|critical", "title": "...", "description": "..."}}
]
```

Règles :
- "title" sous 60 caractères, observable et précis (pas généraliste)
- "description" sous 200 caractères, mentionne les chiffres clés
- Cherche des CORRELATIONS entre sources (sommeil↔dépenses, browsing↔maison, exercise↔stress)
- Si rien de notable, retourne `[]` (tableau vide)
- N'invente AUCUN chiffre absent du JSON ci-dessus
- Pas de répétition des détecteurs basiques (delta dépenses, jours hors maison)
- Sois concret : "tu sors plus tard le vendredi" plutôt que "tendance variable"
"""


async def _llm_cross_source_insights(db: AsyncSession, now: datetime) -> list[Insight]:
    """Genere des insights cross-source via Qwen 2.5 14B local.

    Skip silencieux si Ollama down ou si le LLM retourne rien d'exploitable.
    Limite a 3 insights par run pour eviter la pollution.
    """
    import json as _json

    from src.core.config import get_settings

    settings = get_settings()

    try:
        stats = await _gather_cross_source_stats(db, now)
    except Exception:
        return []

    # Skip si pas assez de data pour etre interessant
    n_sources = sum(1 for k in stats if k != "now" and stats[k])
    if n_sources < 2:
        return []

    prompt = _LLM_INSIGHT_PROMPT.format(stats_json=_json.dumps(stats, ensure_ascii=False))

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 800},
                },
            )
            r.raise_for_status()
            response_text = r.json().get("response", "").strip()
    except Exception:
        return []

    # Parse le JSON retourne (parfois entoure de ```json ... ```)
    if response_text.startswith("```"):
        # Retire fences markdown
        lines = response_text.split("\n")
        response_text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed = _json.loads(response_text)
        if not isinstance(parsed, list):
            return []
    except Exception:
        return []

    out: list[Insight] = []
    for item in parsed[:3]:  # cap a 3
        if not isinstance(item, dict):
            continue
        sev = item.get("severity", "info")
        if sev not in ("critical", "warning", "info", "positive"):
            sev = "info"
        title = str(item.get("title", "")).strip()[:80]
        desc = str(item.get("description", "")).strip()[:240]
        if not title or not desc:
            continue
        out.append(
            Insight(
                severity=sev,
                icon="Sparkles",
                title=title,
                description=desc,
                action="Voir le contexte",
                action_url="/insights",
                source="cross-llm",
                generated_at=now,
            )
        )

    return out


# ---------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------


@router.get("", response_model=InsightsResponse, summary="Insights agreges multi-sources")
async def list_insights(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InsightsResponse:
    now = datetime.now(UTC)
    all_insights: list[Insight] = []

    # On execute en sequence pour partager la session DB
    for fn in (
        _locations_insights,
        _calendar_insights,
        _tasks_insights,
        _finance_insights,
        _emails_insights,
        _health_insights,
        _llm_cross_source_insights,
    ):
        try:
            results = await fn(db, now)
            all_insights.extend(results)
        except Exception as e:
            # Une source en panne ne doit pas casser tout
            all_insights.append(
                Insight(
                    severity="warning",
                    icon="AlertCircle",
                    title=f"Source insights indisponible: {fn.__name__}",
                    description=f"Erreur: {str(e)[:120]}",
                    source="system",
                    generated_at=now,
                )
            )

    # Tri : severity (critical d'abord), puis metric_value desc
    severity_order = {"critical": 0, "warning": 1, "info": 2, "positive": 3}
    all_insights.sort(key=lambda i: (severity_order.get(i.severity, 4), -(i.metric_value or 0)))

    by_sev: dict[str, int] = {}
    for ins in all_insights:
        by_sev[ins.severity] = by_sev.get(ins.severity, 0) + 1

    return InsightsResponse(
        insights=all_insights,
        generated_at=now,
        total=len(all_insights),
        by_severity=by_sev,
    )
