"""Session SQLAlchemy async pour PostgreSQL."""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import get_settings

settings = get_settings()

# echo SQL : opt-in via SQL_ECHO=1 (sinon les requêtes avec data perso loggent
# sur stdout — passwords pas dans les requêtes mais transactions/emails oui)
_sql_echo = os.environ.get("SQL_ECHO", "").lower() in ("1", "true", "yes")

engine = create_async_engine(
    settings.database_url,
    echo=_sql_echo,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession]:
    """Dépendance FastAPI : fournit une session DB par requête."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
