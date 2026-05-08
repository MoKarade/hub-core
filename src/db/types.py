"""Types SQLAlchemy custom partages.

`EmbeddingType` : vecteur float (CLIP, etc.) compatible Postgres (pgvector) +
SQLite (JSON). Fixe la dimension a la creation. Sur Postgres, la migration
Alembic doit aussi creer un index HNSW pour que la recherche cosine soit rapide.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class EmbeddingType(TypeDecorator[list[float]]):
    """Stocke un `list[float]` de dimension fixe.

    Postgres : `vector(N)` via pgvector (avec cast natif depuis text array).
    SQLite/autres : `JSON` (list[float]).
    """

    impl = JSON
    cache_ok = True

    def __init__(self, dimensions: int, *args: Any, **kwargs: Any) -> None:
        self.dimensions = dimensions
        super().__init__(*args, **kwargs)

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector

                return dialect.type_descriptor(Vector(self.dimensions))
            except ImportError:
                pass
        return dialect.type_descriptor(JSON())
