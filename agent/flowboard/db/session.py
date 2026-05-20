"""SQLAlchemy engine + session helper.

Postgres-only since Phase 1. SQLite + runtime ALTER TABLE were removed
when the schema moved to Project → Scene → Shot. The schema is owned by
Alembic; this module never calls ``metadata.create_all()`` in production
boot (only the test conftest does, against the same engine).
"""
from contextlib import contextmanager

from sqlmodel import Session, create_engine

from flowboard.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)


@contextmanager
def get_session():
    with Session(engine) as session:
        yield session
