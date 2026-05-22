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

from flowboard.config import WS_HOST
from flowboard.db import get_session
from flowboard.db.models import Request
from flowboard.routes import (
    activity,
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
    # Schema is owned by Alembic now — run `alembic upgrade head` before
    # boot. We deliberately don't auto-create tables on startup so a
    # missing migration surfaces as a clean failure instead of drifting
    # the schema silently.
    recovered = _recover_orphan_running_requests()
    if recovered:
        logger.info("recovered %d orphan running request(s) → failed", recovered)
    worker = get_worker()
    ws_task = asyncio.create_task(run_ws_server(), name="ext-ws-server")
    worker_task = asyncio.create_task(worker.start(), name="request-worker")
    logger.info("flowboard agent started (ws:9223 + worker)")
    try:
        yield
    finally:
        worker.request_shutdown()
        try:
            await asyncio.wait_for(worker.drain(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("worker drain timed out")
        for t in (ws_task, worker_task):
            t.cancel()
        await asyncio.gather(ws_task, worker_task, return_exceptions=True)
        logger.info("flowboard agent stopped")


app = FastAPI(title="Flowboard Agent", version="0.0.2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(nodes.router)
app.include_router(edges.router)
app.include_router(chat.router)
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
