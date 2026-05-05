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

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    CalendarEvent,
    CreditCardTransaction,
    Email,
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
        delta_h = (ev.start_at - now).total_seconds() / 3600
        delta = f"dans {int(delta_h)}h" if delta_h >= 1 else f"dans {int(delta_h*60)} min"
        out.append(
            Insight(
                severity="info" if delta_h > 12 else "warning",
                icon="Calendar",
                title=ev.summary or "Evenement sans titre",
                description=f"{ev.start_at.strftime('%d/%m %H:%M')} ({ev.location or 'lieu non precise'})",
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
    today = now.date()

    # Overdue (due_at < now ET pas completed)
    q_overdue = select(func.count()).select_from(Task).where(
        Task.is_completed.is_(False),
        Task.due_at.is_not(None),
        Task.due_at < now,
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
    q_pending = select(func.count()).select_from(Task).where(
        Task.is_completed.is_(False)
    )
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
                    description=f"{biggest.transaction_date.isoformat()} - {biggest.description[:80]}",
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
        .where(
            CreditCardTransaction.transaction_date >= today - timedelta(days=120)
        )
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
                description=", ".join(
                    f"{d[:25]} ({float(a):.0f}$)" for d, a, _ in subs[:3]
                ),
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
        days_away = (now - last_home.start_time).days
        if days_away >= 7:
            out.append(
                Insight(
                    severity="warning" if days_away > 30 else "info",
                    icon="Home",
                    title="Loin de chez toi",
                    description=f"Derniere visite HOME : il y a {days_away} jours ({last_home.start_time.date().isoformat()})",
                    delta=f"{days_away} j",
                    action="Voir la journee",
                    action_url=f"/locations?date={last_home.start_time.date().isoformat()}",
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
                    description=f"Le {target.isoformat()} tu etais ici ({float(visit.lat):.3f}, {float(visit.lng):.3f})",
                    delta=f"{years_ago} an(s)",
                    action="Voir la journee",
                    action_url=f"/locations?date={target.isoformat()}",
                    source="locations",
                    metric_value=float(years_ago),
                    generated_at=now,
                )
            )
            break  # un seul anniversaire suffit
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
    all_insights.sort(
        key=lambda i: (severity_order.get(i.severity, 4), -(i.metric_value or 0))
    )

    by_sev: dict[str, int] = {}
    for ins in all_insights:
        by_sev[ins.severity] = by_sev.get(ins.severity, 0) + 1

    return InsightsResponse(
        insights=all_insights,
        generated_at=now,
        total=len(all_insights),
        by_severity=by_sev,
    )
