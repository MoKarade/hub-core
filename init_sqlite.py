"""Init SQLite DB pour dev local sans Docker.
Bypass Alembic (qui a du Postgres-specific) et utilise Base.metadata.create_all()
qui respecte les with_variant() sur les modeles.
"""

import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import get_settings
from src.db import models  # noqa: F401 - load all models
from src.db.base import Base


async def main() -> None:
    settings = get_settings()
    print(f"DATABASE_URL = {settings.database_url}")
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("[OK] Tables creees")


if __name__ == "__main__":
    asyncio.run(main())
