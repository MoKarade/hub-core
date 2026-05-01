"""Init SQLite DB pour dev local sans Docker.

Bypass Alembic (qui a du Postgres-specific) et utilise Base.metadata.create_all()
qui respecte les with_variant() sur les modeles.

NOTE : create_all() ne MIGRE pas les tables existantes (= n'ajoute pas les
nouvelles colonnes des modeles). Pour ca on a une fonction auto_migrate()
qui detecte les colonnes manquantes via PRAGMA table_info et fait des
ALTER TABLE ADD COLUMN.

A relancer apres CHAQUE pull qui ajoute des colonnes a un modele.
"""

import asyncio
import sqlite3

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.sqlite.base import SQLiteDialect
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import get_settings
from src.db import models  # noqa: F401 - load all models
from src.db.base import Base


def auto_migrate_sqlite(db_path: str) -> int:
    """Pour chaque table SQLAlchemy, compare colonnes du modele vs DB existante.
    ALTER TABLE ADD COLUMN pour les colonnes manquantes.

    Retourne le nombre de colonnes ajoutees au total.
    """
    dialect = SQLiteDialect()
    c = sqlite3.connect(db_path)
    cur = c.cursor()
    added = 0

    for table_name, table in Base.metadata.tables.items():
        cur.execute(f"PRAGMA table_info({table_name})")
        existing_cols = {row[1] for row in cur.fetchall()}
        if not existing_cols:
            # Table n'existe pas encore : create_all l'ajoutera, on skip
            continue
        for col in table.columns:
            if col.name in existing_cols:
                continue
            # Compile le type via le dialect SQLite (gere with_variant + JSON, etc.)
            try:
                col_type = col.type.compile(dialect=dialect)
            except Exception:
                col_type = "TEXT"  # fallback safe
            sql = f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}"
            try:
                cur.execute(sql)
                print(f"  ALTER: {table_name}.{col.name} {col_type}")
                added += 1
            except sqlite3.OperationalError as e:
                print(f"  SKIP : {table_name}.{col.name} ({e})")

    c.commit()
    c.close()
    return added


async def main() -> None:
    settings = get_settings()
    print(f"DATABASE_URL = {settings.database_url}")

    # Etape 1 : create_all pour les nouvelles tables
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("[OK] create_all done (new tables)")

    # Etape 2 : auto_migrate pour les colonnes manquantes (tables existantes)
    if "sqlite" in settings.database_url:
        # Extract path apres "sqlite+aiosqlite:///./hub.db"
        db_path = settings.database_url.split("///")[-1]
        print(f"[*] Auto-migrate SQLite : {db_path}")
        added = auto_migrate_sqlite(db_path)
        print(f"[OK] Auto-migrate : {added} colonnes ajoutees")


if __name__ == "__main__":
    asyncio.run(main())
