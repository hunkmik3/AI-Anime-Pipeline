"""Batch 4K upscaler — HTTP surface.

A standalone tool page: upload many images → each is re-rendered at 4K via
Atrium Nano Banana Pro (services/image/atrium_upscale.py), conditioned on the
source + a fixed "clean anime master" prompt. One request PER image so the
frontend can fire the whole batch at once (Atrium has no concurrent limit for
this account), show per-image progress, and re-run (redo) a single result.

Flow per image: cache the uploaded bytes → hoist to a public R2 URL (Atrium
fetches the source server-side) → upscale → cache the 4K PNG result. Output name
= source base name + "_4K" (e.g. ABCD_Ep01_Part01_Shot01 → …_Shot01_4K).

Isolated from the rest of the app: new prefix, no project binding, touches only
the media cache + Atrium.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

from flowboard.db import get_session
from flowboard.db.models import AppSetting, User
from flowboard.routes.deps import get_optional_user
from flowboard.services import media as media_service
from flowboard.services.image import atrium_upscale

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/upscale", tags=["upscale"])

_ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp"}
MAX_UPSCALE_BYTES = 60 * 1024 * 1024  # 60 MB source cap


def _base_name(name: Optional[str]) -> str:
    """Strip the extension for the _4K output name; blank → 'image'."""
    return re.sub(r"\.[^.]+$", "", (name or "").strip()) or "image"


def _hoist_public(media_id: str) -> str:
    """Public URL Atrium fetches the source from. Same approach as GiantFlowStudio:
    prefer the self-hosted /thumb (served through the public tunnel — no R2 round
    trip, and R2's per-connection throttling doesn't apply); fall back to an R2
    presigned URL only if PUBLIC_MEDIA_BASE_URL isn't set. ``/thumb`` serves a
    downscaled JPEG/WebP (≤w) directly (never 302s to a CDN), so Atrium always
    gets the bytes."""
    base = os.getenv("PUBLIC_MEDIA_BASE_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/api/media/{media_id}/thumb?w=2048"
    from flowboard.services.video.dreamina import media_id_to_public_url

    return media_id_to_public_url(media_id)


async def _run_upscale(source_media_id: str) -> str:
    """Hoist source → Atrium 4K → (keep original colour) → cache result. Returns
    the result media_id."""
    public_url = _hoist_public(source_media_id)
    png = await atrium_upscale.upscale_to_4k(public_url)
    # Neutralise the upscale model's red/pink cast: match the 4K result's a*/b*
    # back to the ORIGINAL source (L untouched). Best-effort — never fails the run.
    if atrium_upscale.KEEP_ORIGINAL_COLOR:
        src_path = media_service.cached_path(source_media_id)
        if src_path is not None:
            try:
                src_bytes = src_path.read_bytes()
                png = await asyncio.to_thread(atrium_upscale.match_reference_colors, png, src_bytes)
            except Exception:  # noqa: BLE001
                pass
    result_id = str(uuid.uuid4())
    media_service.ingest_inline_bytes(result_id, png, kind="image", mime="image/png")
    return result_id


@router.get("/config")
def upscale_config():
    """So the UI can tell the user up front if Atrium isn't wired."""
    return {
        "configured": atrium_upscale.is_configured(),
        "model": atrium_upscale.UPSCALE_MODEL,
    }


# ── Per-account projects + workspace + async jobs ────────────────────────────
# Each account has its OWN upscale space — private and following the user across
# browsers/devices (not per-browser like localStorage). It's organised into
# named PROJECTS so old batches can be revisited. Everything lives in the generic
# app_setting key-value table (no new table / migration):
#   upscale_projects:{uid}          -> {"projects":[{id,name,created_at}]}
#   upscale_ws:{uid}                -> the DEFAULT project's items (legacy
#                                      suffix-less key, kept so pre-projects data
#                                      isn't orphaned)
#   upscale_ws:{uid}:{project_id}   -> a named project's items
#   upscale_job:{uid}:{source_id}   -> async 4K job status for one source
# Item JSON is id pointers + names only, never bytes.

_WS_MAX_ITEMS = 2000
DEFAULT_PROJECT_ID = "default"


def _uid(user: Optional[User]) -> str:
    return str(user.id) if user is not None else "_local"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ws_key(user: Optional[User], project_id: str) -> str:
    uid = _uid(user)
    # Default project keeps the original suffix-less key so items saved before
    # projects existed are still found.
    if project_id == DEFAULT_PROJECT_ID:
        return f"upscale_ws:{uid}"
    return f"upscale_ws:{uid}:{project_id}"


def _projects_key(user: Optional[User]) -> str:
    return f"upscale_projects:{_uid(user)}"


def _job_key(uid: str, source_media_id: str) -> str:
    return f"upscale_job:{uid}:{source_media_id}"


# ---- projects ------------------------------------------------------------

def _read_projects(s, user: Optional[User]) -> list[dict]:
    row = s.get(AppSetting, _projects_key(user))
    projects: list[dict] = []
    if row and row.value:
        try:
            data = json.loads(row.value)
            if isinstance(data, dict) and isinstance(data.get("projects"), list):
                projects = [
                    {"id": str(p["id"]), "name": str(p.get("name") or "Project"),
                     "created_at": str(p.get("created_at") or "")}
                    for p in data["projects"]
                    if isinstance(p, dict) and p.get("id")
                ]
        except (ValueError, TypeError, KeyError):
            projects = []
    if not any(p["id"] == DEFAULT_PROJECT_ID for p in projects):
        projects.insert(0, {"id": DEFAULT_PROJECT_ID, "name": "Mặc định", "created_at": _now_iso()})
        _write_projects(s, user, projects)
    return projects


def _write_projects(s, user: Optional[User], projects: list[dict]) -> None:
    key = _projects_key(user)
    payload = json.dumps({"projects": projects})
    row = s.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=payload)
    else:
        row.value = payload
        row.updated_at = datetime.now(timezone.utc)
    s.add(row)
    s.commit()


@router.get("/projects")
def list_projects(user: Optional[User] = Depends(get_optional_user)):
    with get_session() as s:
        return {"projects": _read_projects(s, user)}


class ProjectBody(BaseModel):
    name: str


@router.post("/projects")
def create_project(body: ProjectBody, user: Optional[User] = Depends(get_optional_user)):
    name = (body.name or "").strip()[:120] or "Project"
    proj = {"id": str(uuid.uuid4()), "name": name, "created_at": _now_iso()}
    with get_session() as s:
        projects = _read_projects(s, user)
        projects.append(proj)
        _write_projects(s, user, projects)
    return {"project": proj}


@router.patch("/projects/{project_id}")
def rename_project(project_id: str, body: ProjectBody, user: Optional[User] = Depends(get_optional_user)):
    name = (body.name or "").strip()[:120] or "Project"
    with get_session() as s:
        projects = _read_projects(s, user)
        for p in projects:
            if p["id"] == project_id:
                p["name"] = name
                _write_projects(s, user, projects)
                return {"ok": True}
    raise HTTPException(404, "project not found")


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, user: Optional[User] = Depends(get_optional_user)):
    if project_id == DEFAULT_PROJECT_ID:
        raise HTTPException(400, "cannot delete the default project")
    with get_session() as s:
        projects = [p for p in _read_projects(s, user) if p["id"] != project_id]
        _write_projects(s, user, projects)
        ws_row = s.get(AppSetting, _ws_key(user, project_id))
        if ws_row is not None:
            s.delete(ws_row)
            s.commit()
    return {"ok": True}


# ---- workspace (per project) ---------------------------------------------

def _clean_ws_items(items: list) -> list[dict]:
    """Whitelist the fields we persist (id pointers + names) and drop anything
    without a source media_id. Defensive: this is user-supplied JSON."""
    out: list[dict] = []
    for it in items[:_WS_MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        sid = it.get("sourceMediaId")
        if not isinstance(sid, str) or not sid:
            continue
        rid = it.get("resultMediaId")
        rname = it.get("resultName")
        out.append(
            {
                "key": str(it.get("key") or sid),
                "name": str(it.get("name") or "image"),
                "sourceMediaId": sid,
                "resultMediaId": rid if isinstance(rid, str) and rid else None,
                "resultName": rname if isinstance(rname, str) and rname else None,
            }
        )
    return out


@router.get("/workspace")
def get_workspace(
    project_id: str = DEFAULT_PROJECT_ID, user: Optional[User] = Depends(get_optional_user)
):
    """The saved upscale items (source→4K pointers) for one of the account's
    projects (defaults to the Default project)."""
    key = _ws_key(user, project_id)
    with get_session() as s:
        row = s.get(AppSetting, key)
    if row is None or not row.value:
        return {"items": []}
    try:
        data = json.loads(row.value)
        items = data.get("items") if isinstance(data, dict) else None
        return {"items": _clean_ws_items(items) if isinstance(items, list) else []}
    except (ValueError, TypeError):
        return {"items": []}


class WorkspaceBody(BaseModel):
    items: list[dict]


@router.put("/workspace")
def put_workspace(
    body: WorkspaceBody,
    project_id: str = DEFAULT_PROJECT_ID,
    user: Optional[User] = Depends(get_optional_user),
):
    """Replace one project's workspace (the UI sends its full item list,
    debounced, on every change)."""
    key = _ws_key(user, project_id)
    payload = json.dumps({"items": _clean_ws_items(body.items)})
    with get_session() as s:
        row = s.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=payload)
        else:
            row.value = payload
            row.updated_at = datetime.now(timezone.utc)
        s.add(row)
        s.commit()
    return {"ok": True}


# ---- async 4K jobs (avoids Cloudflare's ~100s 524 on the slow sync call) --
# The Atrium 4K pass takes ~1-2 min — well past Cloudflare's edge timeout. So
# POST /run kicks it off in the BACKGROUND and returns instantly; the UI polls
# GET /job/{source_id}. Job status is keyed by SOURCE media_id (one live job per
# source, re-run overwrites), so concurrent jobs never collide on one row and an
# in-flight upscale reconnects after a reload.

_bg_tasks: set = set()


def _write_job(uid: str, source_media_id: str, data: dict) -> None:
    key = _job_key(uid, source_media_id)
    payload = json.dumps(data)
    with get_session() as s:
        row = s.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=payload)
        else:
            row.value = payload
            row.updated_at = datetime.now(timezone.utc)
        s.add(row)
        s.commit()


async def _run_job(uid: str, source_media_id: str, base: str) -> None:
    try:
        result_id = await _run_upscale(source_media_id)
        _write_job(uid, source_media_id, {
            "status": "done", "result_media_id": result_id, "result_name": f"{base}_4K",
        })
        logger.info("upscale job done: %s -> %s", source_media_id, result_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("upscale job failed for %s: %s", source_media_id, exc)
        _write_job(uid, source_media_id, {"status": "error", "error": str(exc)[:300]})


class RunBody(BaseModel):
    source_media_id: str
    name: Optional[str] = None


@router.post("/run")
async def upscale_run(body: RunBody, user: Optional[User] = Depends(get_optional_user)):
    """Start a 4K upscale in the background and return immediately (no 524)."""
    sid = body.source_media_id
    if not media_service.is_valid_media_id(sid) or media_service.cached_path(sid) is None:
        raise HTTPException(404, "source media not found")
    uid = _uid(user)
    base = _base_name(body.name)
    _write_job(uid, sid, {"status": "running", "started_at": _now_iso(), "name": base})
    task = asyncio.create_task(_run_job(uid, sid, base))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"ok": True, "status": "running"}


@router.get("/job/{source_media_id}")
def upscale_job(source_media_id: str, user: Optional[User] = Depends(get_optional_user)):
    """Poll one source's 4K job. status: running | done | error | none."""
    with get_session() as s:
        row = s.get(AppSetting, _job_key(_uid(user), source_media_id))
    if row is None or not row.value:
        return {"status": "none"}
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) else {"status": "none"}
    except (ValueError, TypeError):
        return {"status": "none"}


@router.post("/image")
async def upscale_image(file: UploadFile = File(...), name: Optional[str] = Form(None)):
    """Upload one image + upscale it. Returns both the cached source and the 4K
    result (with its _4K name) so the UI can render input↔output side by side."""
    mime = (file.content_type or "").lower().split(";")[0].strip()
    if mime not in _ALLOWED_MIMES:
        raise HTTPException(415, f"unsupported image type: {mime!r}")
    raw = await file.read(MAX_UPSCALE_BYTES + 1)
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > MAX_UPSCALE_BYTES:
        raise HTTPException(413, f"file too large: {len(raw)} > {MAX_UPSCALE_BYTES}")

    src_id = str(uuid.uuid4())
    if not media_service.ingest_inline_bytes(src_id, raw, kind="image", mime=mime):
        raise HTTPException(500, "failed to cache source image")
    base = _base_name(name or file.filename or "image")
    try:
        result_id = await _run_upscale(src_id)
    except atrium_upscale.UpscaleError as exc:
        raise HTTPException(502, f"upscale failed: {exc}")
    logger.info("upscale: %s -> %s (%s_4K)", src_id, result_id, base)
    return {
        "source_media_id": src_id,
        "source_name": base,
        "result_media_id": result_id,
        "result_name": f"{base}_4K",
    }


@router.post("/source")
async def upscale_source(file: UploadFile = File(...), name: Optional[str] = Form(None)):
    """Cache an uploaded source image and return its media_id — WITHOUT upscaling.

    The UI uploads on *add* (not on upscale) so the source lives on the server
    immediately: the page can then persist just the media_id and survive a reload
    / the tab being closed. The actual 4K pass runs later via ``/redo`` from this
    id. Same mime/size guards as ``/image``."""
    mime = (file.content_type or "").lower().split(";")[0].strip()
    if mime not in _ALLOWED_MIMES:
        raise HTTPException(415, f"unsupported image type: {mime!r}")
    raw = await file.read(MAX_UPSCALE_BYTES + 1)
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > MAX_UPSCALE_BYTES:
        raise HTTPException(413, f"file too large: {len(raw)} > {MAX_UPSCALE_BYTES}")
    src_id = str(uuid.uuid4())
    if not media_service.ingest_inline_bytes(src_id, raw, kind="image", mime=mime):
        raise HTTPException(500, "failed to cache source image")
    base = _base_name(name or file.filename or "image")
    logger.info("upscale source cached: %s (%s)", src_id, base)
    return {"source_media_id": src_id, "source_name": base}


class RedoBody(BaseModel):
    source_media_id: str
    name: Optional[str] = None


@router.post("/redo")
async def upscale_redo(body: RedoBody):
    """Re-run the upscale for an existing source (the '↻ redo' on a result the
    user didn't like). Mints a fresh 4K result from the same cached source."""
    if (
        not media_service.is_valid_media_id(body.source_media_id)
        or media_service.cached_path(body.source_media_id) is None
    ):
        raise HTTPException(404, "source media not found")
    base = _base_name(body.name)
    try:
        result_id = await _run_upscale(body.source_media_id)
    except atrium_upscale.UpscaleError as exc:
        raise HTTPException(502, f"upscale failed: {exc}")
    return {"result_media_id": result_id, "result_name": f"{base}_4K"}


class ZipItem(BaseModel):
    media_id: str
    name: str  # the _4K name, without extension


@router.post("/zip")
def upscale_zip(items: list[ZipItem]):
    """Bundle all chosen 4K results into one .zip, each named <name>.png
    (name already carries the _4K suffix). ZIP_STORED — PNGs are already
    compressed, so re-deflating just burns CPU."""
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for it in items:
            p = media_service.cached_path(it.media_id)
            if p is None:
                continue
            safe = re.sub(r'[\\/:*?"<>|]+', "_", it.name).strip() or it.media_id
            arc = f"{safe}.png"
            n = 1
            while arc in used:
                n += 1
                arc = f"{safe}_{n}.png"
            used.add(arc)
            zf.write(str(p), arcname=arc)
    data = buf.getvalue()
    if not data or not used:
        raise HTTPException(404, "no result images to zip")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="upscaled_4K.zip"'},
    )
