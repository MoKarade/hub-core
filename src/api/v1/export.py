"""Endpoint /v1/export/* — export complet des donnees du hub (Phase 6+).

Marc applique a lui-meme le droit d'acces Loi 25 / RGPD : il peut telecharger
TOUTES ses donnees du hub en 1 ZIP. Utile pour :
- Sauvegarde manuelle ponctuelle
- Migration vers un autre PC
- Audit de ce que le hub contient

Format : ZIP streame avec 1 CSV par table + 1 manifest.json.

Pas de PII filtering : c'est SES donnees, il a le droit de tout voir.
Body emails exclu par defaut (taille) — option `include_email_bodies=true` pour les inclure.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.rate_limit import rate_limit
from src.db.models import (
    Account,
    CalendarEvent,
    Contact,
    CreditCardTransaction,
    DriveFile,
    Email,
    HealthMetric,
    InvestmentPosition,
    InvestmentTransaction,
    LocationActivity,
    LocationAddress,
    LocationPoint,
    LocationVisit,
    NamedPlace,
    NewsArticle,
    Photo,
    RemovalRequest,
    SocialPost,
    StreamingActivity,
    Task,
    Transaction,
    YouTubeActivity,
)
from src.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/export", tags=["export"])
_OWNER_EMAIL: str = get_settings().hub_owner_email


# ─────────────────────────────────────────────────────────────────────────
# Tables a exporter : (filename, model, exclude_cols)
# Colonnes exclues = body / blob / encrypted (pas pertinents pour CSV)
# ─────────────────────────────────────────────────────────────────────────


# (filename.csv, ORM model, optional list of cols to exclude)
EXPORT_TABLES: list[tuple[str, type, list[str]]] = [
    ("accounts.csv", Account, []),
    ("transactions.csv", Transaction, []),
    ("credit_card_transactions.csv", CreditCardTransaction, []),
    ("investment_transactions.csv", InvestmentTransaction, []),
    ("investment_positions.csv", InvestmentPosition, []),
    ("location_visits.csv", LocationVisit, []),
    ("location_activities.csv", LocationActivity, []),
    ("location_points.csv", LocationPoint, []),
    ("location_addresses.csv", LocationAddress, []),
    ("named_places.csv", NamedPlace, []),
    ("calendar_events.csv", CalendarEvent, []),
    ("contacts.csv", Contact, []),
    ("tasks.csv", Task, []),
    ("health_metrics.csv", HealthMetric, []),
    ("photos.csv", Photo, []),
    ("drive_files.csv", DriveFile, []),
    ("youtube_activities.csv", YouTubeActivity, []),
    ("news_articles.csv", NewsArticle, []),
    ("streaming_activities.csv", StreamingActivity, []),
    ("removal_requests.csv", RemovalRequest, []),
    ("social_posts.csv", SocialPost, []),
]


async def _table_to_csv(
    db: AsyncSession,
    model: type,
    exclude_cols: list[str],
) -> tuple[bytes, int]:
    """Export 1 table en CSV bytes. Retourne (data, row_count)."""
    cols = [c.key for c in model.__table__.columns if c.key not in exclude_cols]
    stmt = select(model)
    rows = (await db.execute(stmt)).scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(cols)
    for row in rows:
        line = []
        for c in cols:
            val = getattr(row, c, None)
            if val is None:
                line.append("")
            elif isinstance(val, datetime):
                line.append(val.isoformat())
            elif isinstance(val, list | dict):
                line.append(json.dumps(val, ensure_ascii=False, default=str))
            elif isinstance(val, bytes):
                line.append(f"<bytes:{len(val)}>")  # don't dump encrypted blobs
            else:
                line.append(str(val))
        writer.writerow(line)
    return buf.getvalue().encode("utf-8"), len(rows)


@router.get("/all", dependencies=[Depends(rate_limit(2, 300))])
async def export_all(
    db: Annotated[AsyncSession, Depends(get_db)],
    confirm: Annotated[
        str,
        Query(description="Doit valoir 'oui' pour confirmer l'export total"),
    ] = "",
    include_email_bodies: Annotated[
        bool,
        Query(description="Inclure le corps des emails (gros)"),
    ] = False,
) -> StreamingResponse:
    """Telecharge TOUT le contenu du hub en 1 ZIP.

    1 CSV par table + manifest.json avec counts + generated_at.
    Tokens OAuth chiffres = exclus (pas exportable en clair, par design).

    Requiert ?confirm=oui pour éviter les déclenchements accidentels.
    """
    if confirm != "oui":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Paramètre ?confirm=oui requis pour déclencher l'export total.",
        )
    started = datetime.now(UTC)

    # On streame le ZIP en memoire (simpler que tempfile, OK pour <100 MB)
    zip_buffer = io.BytesIO()
    counts: dict[str, int] = {}

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, model, exclude in EXPORT_TABLES:
            extra_exclude = list(exclude)
            if model is Email and not include_email_bodies:
                extra_exclude += ["body_text", "body_html"]

            try:
                csv_bytes, n = await _table_to_csv(db, model, extra_exclude)
                zf.writestr(filename, csv_bytes)
                counts[filename] = n
            except Exception as e:
                logger.warning("export_table_failed table=%s err=%r", filename, e)
                counts[filename] = -1

        # Email separe pour inclure conditionnellement le body
        try:
            extra = [] if include_email_bodies else ["body_text", "body_html"]
            csv_bytes, n = await _table_to_csv(db, Email, extra)
            zf.writestr("emails.csv", csv_bytes)
            counts["emails.csv"] = n
        except Exception as e:
            logger.warning("export_emails_failed err=%r", e)
            counts["emails.csv"] = -1

        manifest: dict[str, Any] = {
            "generated_at": started.isoformat(),
            "user_email": _OWNER_EMAIL,
            "include_email_bodies": include_email_bodies,
            "tables": counts,
            "total_rows": sum(c for c in counts.values() if c > 0),
            "notes": (
                "Export complet du Personal Data Hub. "
                "Tokens OAuth chiffres exclus (par design). "
                "Pour re-importer, utiliser hub-ingest/scripts/import_export.py "
                "(non implemente : a coder si besoin)."
            ),
        }
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )

        # README humain dans le ZIP
        readme = f"""# Personal Data Hub — Export complet

Genere le {started.strftime("%Y-%m-%d %H:%M:%S UTC")}.

Ce ZIP contient TOUTES les donnees du hub de Marc, exportees en CSV
(une table = un fichier) plus un manifest.json avec les compteurs.

## Contenu

{chr(10).join(f"- {k:35s} {v:8d} lignes" for k, v in sorted(counts.items()) if v >= 0)}

Total : {sum(c for c in counts.values() if c > 0)} lignes.

## Pour ouvrir

- **CSV** : tableur (LibreOffice, Excel, Google Sheets) ou pandas/duckdb pour analyse
- **manifest.json** : metadata generation
- **emails.csv** : {"avec corps" if include_email_bodies else "metadata seulement"}

## Confidentialite

Ces donnees sont 100% les votres. Stockez ce ZIP dans un endroit sur :
votre PC personnel ou un cloud chiffre. Ne le partagez avec personne.
"""
        zf.writestr("README.txt", readme.encode("utf-8"))

    zip_buffer.seek(0)
    filename = f"hub-export-{started.strftime('%Y%m%d-%H%M%S')}.zip"

    elapsed = (datetime.now(UTC) - started).total_seconds()
    logger.info(
        "export_all_done rows=%d duration=%.1fs",
        sum(c for c in counts.values() if c > 0),
        elapsed,
    )

    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Total-Rows": str(sum(c for c in counts.values() if c > 0)),
            "X-Duration-Seconds": str(round(elapsed, 1)),
        },
    )


@router.get("/preview")
async def export_preview(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Apercu de ce qu'un export contiendrait (counts par table) sans generer le ZIP."""
    from sqlalchemy import func

    counts: dict[str, int] = {}
    for filename, model, _ in EXPORT_TABLES:
        try:
            n = (await db.execute(select(func.count()).select_from(model))).scalar_one()
            counts[filename] = int(n or 0)
        except Exception:
            counts[filename] = -1
    # Email separe
    try:
        n = (await db.execute(select(func.count()).select_from(Email))).scalar_one()
        counts["emails.csv"] = int(n or 0)
    except Exception:
        counts["emails.csv"] = -1

    return {
        "tables": counts,
        "total_rows": sum(c for c in counts.values() if c > 0),
    }
