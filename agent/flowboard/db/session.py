"""SQLAlchemy engine + session helper.

Two engines are supported:

- **Postgres** (dev/prod) — schema owned by Alembic; this module never calls
  ``metadata.create_all()`` on that path so a missing migration surfaces loudly.
- **SQLite** (the self-contained desktop build) — there's no Alembic/Docker on
  the target machine, so ``init_db()`` creates the schema from
  ``SQLModel.metadata`` on first run. Called from app startup when the URL is
  SQLite (see ``main.py``).
"""
import os
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine

from flowboard.config import DATABASE_URL

_IS_SQLITE = DATABASE_URL.startswith("sqlite")

# SQLite needs check_same_thread=False (the worker + request handlers touch the
# connection from different threads). pool_pre_ping is a Postgres-ism — it does
# nothing useful for a local SQLite file, so skip it there.
_engine_kwargs: dict = {"echo": False}
if _IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Multi-user: the worker runs many generations concurrently. Each job holds
    # a connection only briefly (mark running / write result — the long provider
    # poll runs with the session CLOSED), but a burst of finishing jobs can grab
    # many at once, so give the pool real headroom. Overridable via env.
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_size"] = int(os.getenv("FLOWBOARD_DB_POOL_SIZE", "20"))
    _engine_kwargs["max_overflow"] = int(os.getenv("FLOWBOARD_DB_MAX_OVERFLOW", "40"))
    _engine_kwargs["pool_timeout"] = 30
    _engine_kwargs["pool_recycle"] = 1800

engine = create_engine(DATABASE_URL, **_engine_kwargs)


if _IS_SQLITE:
    # WAL keeps concurrent reads from blocking the worker's writes — important
    # because the single SQLite file is shared by the API + worker threads.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def init_db() -> None:
    """Create the schema from SQLModel metadata. SQLite-only — Postgres uses
    Alembic. Idempotent (``create_all`` skips existing tables)."""
    if not _IS_SQLITE:
        return
    # Import models so every table is registered on SQLModel.metadata.
    from flowboard.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session():
    with Session(engine) as session:
        yield session
