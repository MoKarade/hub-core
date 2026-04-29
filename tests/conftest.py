"""Configuration pytest commune.

Pour les tests qui touchent la DB, on utilise SQLite in-memory async
(aiosqlite) avec un override de la dépendance `get_db` de FastAPI.

⚠️ SQLite ne supporte pas tout ce que Postgres supporte (ex: tableaux,
JSON typés, certaines fonctions). Pour les tests d'endpoints classiques
CRUD + filtres c'est suffisant.
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.db.base import Base
# Import explicite des modèles pour que metadata.create_all les voie
from src.db.models import (  # noqa: F401
    Account,
    CreditCardTransaction,
    InvestmentPosition,
    InvestmentTransaction,
    LocationPoint,
    Transaction,
)
from src.db.session import get_db
from src.main import app


@pytest_asyncio.fixture
async def engine():
    """Engine SQLite in-memory pour la durée d'un test."""
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine) -> AsyncGenerator[AsyncSession, None]:
    """Session DB scopée au test."""
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client(engine) -> AsyncGenerator[AsyncClient, None]:
    """Client HTTP avec dépendance get_db overridée vers SQLite."""
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_db_override() -> AsyncGenerator[AsyncSession, None]:
        async with SessionLocal() as session:
            try:
                yield session
            finally:
                await session.close()

    app.dependency_overrides[get_db] = _get_db_override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def make_dedup_hash(seed: str) -> str:
    """Genère un dedup_hash 64-char à partir d'une seed lisible."""
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


@pytest.fixture
def dedup() -> Any:
    """Helper rapide pour générer un dedup_hash dans les tests."""
    return make_dedup_hash
