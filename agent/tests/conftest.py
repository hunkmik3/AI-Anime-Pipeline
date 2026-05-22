"""Shared test fixtures.

Postgres is the only supported engine since Phase 1. The test harness
uses a dedicated DB (`flowboard_test`) on the same container as dev, and
TRUNCATEs all tables before each test for isolation.

To run the suite locally: `docker compose up -d postgres`, then
`make install-dev` and `pytest -q`.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Point Flowboard at an isolated test DB BEFORE importing the app. The
# test DB lives on the same Postgres container as dev (created by the
# fixture below) so devs don't need a second container.
_TEST_DB_URL = os.getenv(
    "FLOWBOARD_TEST_DATABASE_URL",
    "postgresql+psycopg://flowboard:flowboard@localhost:15432/flowboard_test",
)
os.environ["FLOWBOARD_DATABASE_URL"] = _TEST_DB_URL

_TMPDIR = tempfile.mkdtemp(prefix="flowboard-test-")
os.environ["FLOWBOARD_STORAGE"] = _TMPDIR
# Force the deterministic mock planner in tests — never spawn `claude` subprocess.
os.environ["FLOWBOARD_PLANNER_BACKEND"] = "mock"

# Hermetic credentials. Phase 6.5 made .env the canonical source for
# API keys / R2 — the agent calls load_dotenv() at boot, and the new
# get_api_key / read_r2_config helpers prefer env vars over secrets.json.
# Without this scrub, a developer running pytest would pick up the real
# .env credentials and hit live providers during tests.
#
# Disable the load_dotenv call inside flowboard.main and strip any
# credential env vars that may have leaked in from the shell. Tests
# that need a key monkeypatch it explicitly.
os.environ["FLOWBOARD_DISABLE_DOTENV"] = "1"
for _v in (
    "BYTEPLUS_KEY",
    "DREAMINA_API_KEY",
    "R2_ENDPOINT_URL",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_PUBLIC_BASE_URL",
):
    os.environ.pop(_v, None)


def _ensure_test_database() -> None:
    """Create `flowboard_test` on the dev container if it doesn't exist."""
    import psycopg

    # Connect to the dev DB to issue CREATE DATABASE (Postgres requires
    # autocommit + a separate connection for that statement).
    admin_url = _TEST_DB_URL.replace("/flowboard_test", "/flowboard").replace(
        "postgresql+psycopg://", "postgresql://"
    )
    with psycopg.connect(admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'flowboard_test'")
            if cur.fetchone() is None:
                cur.execute("CREATE DATABASE flowboard_test")


_ensure_test_database()

# Run Alembic on the test DB before the app + tests import models.
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_cfg = Config(str(_ALEMBIC_INI))
_cfg.set_main_option("sqlalchemy.url", _TEST_DB_URL)
# Wipe + re-create schema each pytest session so migration changes are
# always reflected. Cheap on Postgres (drops the schema, runs the single
# initial migration; ~150ms on a warm container).
from sqlalchemy import create_engine, text  # noqa: E402

_engine = create_engine(_TEST_DB_URL)
with _engine.begin() as _c:
    _c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
    _c.execute(text("CREATE SCHEMA public"))
_engine.dispose()
command.upgrade(_cfg, "head")

from fastapi.testclient import TestClient  # noqa: E402

from flowboard.db.session import engine  # noqa: E402
from flowboard.main import app  # noqa: E402


# Cached list of user tables to TRUNCATE between tests.
def _user_tables() -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename != 'alembic_version'"
            )
        ).all()
        return [r[0] for r in rows]


_TABLES_CACHE: list[str] = []


@pytest.fixture(autouse=True)
def _fresh_db():
    """TRUNCATE all user tables before each test so state is isolated.

    Faster than DROP+CREATE: skips the migration replay entirely while
    still giving the same fresh-slate guarantee SQLite's drop_all gave.
    RESTART IDENTITY resets sequences so node.id, edge.id etc. start at
    1 in every test (some assertions hardcode small ids).
    """
    global _TABLES_CACHE
    if not _TABLES_CACHE:
        _TABLES_CACHE = _user_tables()
    if _TABLES_CACHE:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE TABLE "
                    + ", ".join(f'"{t}"' for t in _TABLES_CACHE)
                    + " RESTART IDENTITY CASCADE"
                )
            )
    yield


@pytest.fixture(autouse=True)
def _seed_default_paygate_tier():
    """Most tests exercise downstream behaviour (variant_count, ref_media_ids,
    SDK payload shape, etc.) and don't care about the upstream tier-resolution
    chain. Pre-Phase-1, the worker silently defaulted to PAYGATE_TIER_ONE when
    no signal was present, so tests didn't have to think about tier at all.
    Phase 1 made that fail loud — every gen now requires a tier signal — so
    we keep the test-time ergonomics by simulating the "extension already
    sniffed Pro" state by default. Tests that specifically want to exercise
    the no-tier path (e.g. test_processor_tier_fallback.py) reset the cache
    in their own module-local autouse fixture, which runs after this one and
    wins.
    """
    from flowboard.services.flow_client import flow_client
    flow_client._paygate_tier = "PAYGATE_TIER_ONE"
    yield
    flow_client._paygate_tier = None


@pytest.fixture
def client():
    return TestClient(app)


def make_shot(client, name: str = "Test") -> dict:
    """Test helper: create Project → Scene → Shot via the new REST surface
    and return a dict shaped like the old `/api/boards` response.

    Pre-Phase-4 tests called `client.post("/api/boards", ...)` to spin up
    a board (= shot under the shim). Phase 4 removed `/api/boards/*`, so
    every legacy test creates the same Project + Scene + Shot pyramid
    explicitly. The returned dict carries the same `id` (shot UUID),
    `project_id`, `name` and `created_at` keys so call sites that read
    those fields keep working unchanged.
    """
    proj = client.post("/api/projects", json={"name": name}).json()
    scene = client.post(
        f"/api/projects/{proj['id']}/scenes",
        json={"name": "Scene 1"},
    ).json()
    shot = client.post(f"/api/scenes/{scene['id']}/shots", json={}).json()
    return {
        "id": shot["id"],
        "project_id": proj["id"],
        "name": proj["name"],
        "created_at": shot.get("created_at"),
        "scene_id": scene["id"],
    }
