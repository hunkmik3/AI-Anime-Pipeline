from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent.parent.parent


def _default_storage() -> Path:
    # Frozen build: ROOT points inside the read-only PyInstaller extract dir,
    # so persist data NEXT TO the executable instead (portable — copy the exe
    # and its data folder together). Source checkout: repo ./storage.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return ROOT / "storage"


STORAGE_DIR = Path(os.getenv("FLOWBOARD_STORAGE") or _default_storage())


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Bundled = running as a self-contained desktop binary (PyInstaller sets
# ``sys.frozen``) or forced via FLOWBOARD_BUNDLED=1. In this mode there's no
# Postgres/Docker on the target machine — everything (DB + media cache) lives
# on the user's disk under STORAGE_DIR.
BUNDLED = bool(getattr(sys, "frozen", False)) or _env_flag("FLOWBOARD_BUNDLED")

# Database connection string. Precedence: explicit FLOWBOARD_DATABASE_URL >
# SQLite-on-disk (bundled desktop build) > Postgres (dev/prod). The
# docker-compose.yml binds Postgres to host port 15432 (5432 was busy on the
# dev machine). The bundled SQLite file is created on first run via
# ``db.session.init_db`` (schema from SQLModel.metadata, no Alembic needed).
def _default_database_url() -> str:
    if BUNDLED:
        return f"sqlite:///{(STORAGE_DIR / 'flowboard.db').as_posix()}"
    return "postgresql+psycopg://flowboard:flowboard@localhost:15432/flowboard"


DATABASE_URL = os.getenv("FLOWBOARD_DATABASE_URL") or _default_database_url()

HTTP_PORT = int(os.getenv("FLOWBOARD_HTTP_PORT", "8101"))
WS_HOST = os.getenv("FLOWBOARD_WS_HOST", "127.0.0.1")
EXTENSION_WS_PORT = int(os.getenv("FLOWBOARD_EXT_WS_PORT", "9223"))


# Flow Chrome-extension bridge (the :9223 WS server). Set
# FLOWBOARD_DISABLE_BRIDGE=1 to skip starting it — used when running video gen
# entirely on the Dreamina/Seedance API, which needs no extension. Defaults OFF
# in a bundled desktop build (no Chrome extension on the target machine);
# FLOWBOARD_DISABLE_BRIDGE=0 force-re-enables it.
BRIDGE_ENABLED = not _env_flag("FLOWBOARD_DISABLE_BRIDGE", default=BUNDLED)

# Multi-user (Phase 9): when on, every data API requires a valid login token
# and projects are scoped to their owner. Off by default so single-user/dev and
# the test suite stay open; a multi-user deployment sets FLOWBOARD_REQUIRE_AUTH=1.
REQUIRE_AUTH = _env_flag("FLOWBOARD_REQUIRE_AUTH")

# Process-wide default video model (registry key). Per-project
# (project.settings.default_video_model) and per-node (data.videoModelId)
# overrides still win above this. Bundled desktop build defaults to Seedance
# via Avis (Flow is off there); dev defaults to Flow.
DEFAULT_VIDEO_MODEL = os.getenv("FLOWBOARD_DEFAULT_VIDEO_MODEL") or (
    "seedance-2-0" if BUNDLED else "flow-default"
)

# Built frontend (Vite `dist`) for the single-process / bundled build. When
# present, FastAPI serves the SPA at "/" so there's no separate Vite server.
# - Frozen (PyInstaller): bundled under sys._MEIPASS/frontend_dist.
# - Source checkout: frontend/dist (exists only after `npm run build`).
# None in normal dev (Vite on :5173 serves the UI), so serving stays inactive.
def _frontend_dist() -> "Path | None":
    if getattr(sys, "frozen", False):
        cand = Path(getattr(sys, "_MEIPASS", ROOT)) / "frontend_dist"
        return cand if (cand / "index.html").exists() else None
    cand = ROOT / "frontend" / "dist"
    return cand if (cand / "index.html").exists() else None


FRONTEND_DIST = _frontend_dist()

PLANNER_MODEL = os.getenv("FLOWBOARD_PLANNER_MODEL", "claude-sonnet-4-6")
# "cli" → always use claude CLI; "mock" → always mock; "auto" → CLI if available,
# otherwise mock. Default auto.
PLANNER_BACKEND = os.getenv("FLOWBOARD_PLANNER_BACKEND", "auto")

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
