# Load .env BEFORE any flowboard.* import so config / secrets see the
# values. .env is the canonical source of truth for runtime config
# (API keys, R2 credentials, FLOWBOARD_* tunables). Anything not in the
# file falls back to the shell environment, then to ~/.flowboard/
# secrets.json (legacy). See docs/r2_setup.md §5 for the contract.
#
# find_dotenv(usecwd=True) walks up from the current working directory
# so the agent picks up .env whether invoked from repo root or from
# agent/. override=False means the shell env wins over .env, matching
# twelve-factor conventions and letting CI / docker injection override
# the dev file.
#
# The conftest.py for pytest sets FLOWBOARD_DISABLE_DOTENV=1 so test
# runs stay hermetic (production credentials in .env never leak into
# the test process even though tests import flowboard.main).
import os as _os

from dotenv import find_dotenv as _find_dotenv, load_dotenv as _load_dotenv

if not _os.environ.get("FLOWBOARD_DISABLE_DOTENV"):
    _dotenv_path = _find_dotenv(usecwd=True)
    if _dotenv_path:
        _load_dotenv(_dotenv_path, override=False)

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware

from flowboard.config import WS_HOST, BRIDGE_ENABLED, FRONTEND_DIST, REQUIRE_AUTH
from flowboard.db import get_session, init_db
from flowboard.db.models import Request
from flowboard.routes import (
    account,
    activity,
    admin,
    auth,
    bibles,
    chat,
    edges,
    llm,
    media,
    nodes,
    plans,
    projects,
    prompt,
    scenes,
    shots,
    upload,
    video_providers,
    vision,
)
from flowboard.routes import references as references_route
from flowboard.routes import requests as requests_route
from flowboard.services.flow_client import flow_client
from flowboard.services.ws_server import run_ws_server
from flowboard.worker.processor import get_worker

# Guard rail: the dedicated WS server is unauthenticated and would expose the
# callback secret to any process that can reach it. Refuse to boot if someone
# overrode WS_HOST to a non-loopback address.
if WS_HOST not in ("127.0.0.1", "localhost", "::1"):
    raise RuntimeError(
        f"FLOWBOARD_WS_HOST must be loopback (got {WS_HOST!r}); the extension WS "
        "is unauthenticated by design and must not be network-reachable."
    )

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _recover_orphan_running_requests() -> int:
    """Mark any pre-existing 'running' requests as failed so a restart doesn't
    leave nodes polling a request that nobody is processing anymore."""
    from datetime import datetime, timezone
    from sqlmodel import select as _select

    touched = 0
    with get_session() as s:
        rows = s.exec(_select(Request).where(Request.status == "running")).all()
        for r in rows:
            r.status = "failed"
            r.error = "agent_restart_lost"
            r.finished_at = datetime.now(timezone.utc)
            s.add(r)
            touched += 1
        if touched:
            s.commit()
    return touched


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Postgres schema is owned by Alembic — run `alembic upgrade head` before
    # boot; we don't auto-create on that path so a missing migration surfaces
    # loudly. The bundled SQLite build has no Alembic, so init_db() creates the
    # schema from SQLModel.metadata on first run (no-op on Postgres).
    init_db()
    # Multi-user (Phase 9): seed the first admin from FLOWBOARD_ADMIN_USER/
    # PASSWORD when the accounts table is empty. No-op once any user exists.
    from flowboard.services import user_service

    user_service.ensure_bootstrap_admin()
    recovered = _recover_orphan_running_requests()
    if recovered:
        logger.info("recovered %d orphan running request(s) → failed", recovered)
    worker = get_worker()
    # Flow bridge is opt-out via FLOWBOARD_DISABLE_BRIDGE — skip the :9223 WS
    # server entirely when running video gen on the Dreamina/Seedance API.
    ws_task = (
        asyncio.create_task(run_ws_server(), name="ext-ws-server")
        if BRIDGE_ENABLED
        else None
    )
    worker_task = asyncio.create_task(worker.start(), name="request-worker")
    if BRIDGE_ENABLED:
        logger.info("flowboard agent started (ws:9223 + worker)")
    else:
        logger.warning(
            "flowboard agent started (worker only) — Flow bridge DISABLED "
            "via FLOWBOARD_DISABLE_BRIDGE; video gen routes to the configured "
            "non-Flow model"
        )
    try:
        yield
    finally:
        worker.request_shutdown()
        try:
            await asyncio.wait_for(worker.drain(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("worker drain timed out")
        tasks = [t for t in (ws_task, worker_task) if t is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("flowboard agent stopped")


app = FastAPI(title="Flowboard Agent", version="0.0.2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Multi-user (Phase 9): when FLOWBOARD_REQUIRE_AUTH is on, every /api call needs
# a valid login token except login + health (+ the extension callback). Off by
# default so single-user/dev and tests stay open. Routes still scope by owner
# via get_optional_user.
from fastapi.responses import JSONResponse as _JSONResponse  # noqa: E402
from flowboard.services import auth as _auth  # noqa: E402

_AUTH_OPEN_PATHS = {"/api/account/login", "/api/health"}


@app.middleware("http")
async def _auth_gate(request: FastAPIRequest, call_next):
    if REQUIRE_AUTH and request.method != "OPTIONS":
        path = request.url.path
        if (
            path.startswith("/api/")
            and path not in _AUTH_OPEN_PATHS
            and not path.startswith("/api/ext/")
        ):
            authz = request.headers.get("authorization") or ""
            uid = (
                _auth.verify_token(authz[7:].strip())
                if authz.lower().startswith("bearer ")
                else None
            )
            if not uid:
                return _JSONResponse({"detail": "authentication required"}, status_code=401)
    return await call_next(request)


app.include_router(nodes.router)
app.include_router(edges.router)
app.include_router(chat.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(projects.router)
app.include_router(scenes.router)
app.include_router(shots.router)
app.include_router(bibles.router)
app.include_router(references_route.router)
app.include_router(requests_route.router)
app.include_router(media.bytes_router)
app.include_router(media.api_router)
app.include_router(upload.router)
app.include_router(plans.router)
app.include_router(vision.router)
app.include_router(prompt.router)
app.include_router(auth.router)
app.include_router(llm.router)
app.include_router(activity.router)
app.include_router(video_providers.router)


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "extension_connected": flow_client.connected,
        "ws_stats": flow_client.ws_stats,
    }


@app.post("/api/ext/callback")
async def ext_callback(
    body: FastAPIRequest,
    x_callback_secret: str | None = Header(default=None, alias="X-Callback-Secret"),
) -> dict:
    """HTTP callback for the extension to deliver API responses."""
    if not x_callback_secret or not hmac.compare_digest(
        x_callback_secret, flow_client.callback_secret
    ):
        raise HTTPException(status_code=401, detail="invalid callback secret")

    try:
        payload = await body.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")

    if not isinstance(payload, dict) or "id" not in payload:
        raise HTTPException(status_code=400, detail="missing id")

    matched = flow_client.resolve_callback(payload)
    return {"ok": matched}


# ── Frontend SPA (single-process / bundled build) ────────────────────────
# Registered LAST so the API/media/ws routers above always take precedence.
# Inactive in normal dev (no built dist → FRONTEND_DIST is None; Vite serves
# the UI on :5173). Active in the bundled desktop build and after `npm run
# build`, where FastAPI serves the SPA on the same port as the API.
if FRONTEND_DIST is not None:
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    _assets_dir = FRONTEND_DIST / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

    @app.get("/{full_path:path}")
    async def _spa_fallback(full_path: str):
        # Never shadow the API surface — let unknown /api,/media,/ws 404 as JSON.
        if full_path.startswith(("api/", "media/", "ws")):
            raise HTTPException(status_code=404, detail="not found")
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
