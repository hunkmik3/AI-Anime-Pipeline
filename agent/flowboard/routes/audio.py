"""Audio generation routes — BytePlus Seed Audio 1.0 (voice + music + SFX).

Synchronous: the provider returns the finished clip inline, so this ingests it
as a local audio media_id and returns it (same shape the AudioRefNode uses), so
the generated audio can be played, downloaded, or wired into a video's @audio.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from flowboard.db import get_session
from flowboard.routes.deps import get_optional_user
from flowboard.services import resource_guard, seed_audio
from flowboard.services.seed_audio import SeedAudioError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audio", tags=["audio"])

# A provider "auth" failure (bad/expired Seed Audio credentials) is a SERVER /
# upstream problem, NOT the user's own session — so DON'T return 401 here: the
# frontend's global 401 handler would log the user out to /login. Map it to 502
# (bad gateway) so the UI surfaces a real error instead.
_STATUS_BY_CODE = {"auth": 502, "bad_input": 400, "quota": 429, "internal": 502}


class SeedAudioRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=seed_audio.MAX_PROMPT_CHARS)
    format: str = "mp3"                 # wav | mp3 | pcm | ogg_opus
    sample_rate: int = 24000
    speech_rate: int = 0                # -50..100 (0 = normal), clamped provider-side
    loudness_rate: int = 0
    pitch_rate: int = 0                 # -12..12
    references: Optional[list[str]] = None   # ≤3 audio media_ids or public URLs → @audio1..3
    image_ref: Optional[str] = None          # 1 image media_id/URL (mutually exclusive w/ audio)
    node_id: Optional[int] = None


@router.get("/seed/available")
async def seed_audio_available() -> dict:
    """Cheap probe so the UI can show/hide the node's Generate button."""
    return {"available": seed_audio.is_configured()}


@router.post("/generate")
async def generate_audio(body: SeedAudioRequest, user=Depends(get_optional_user)) -> dict:
    # The clip is written onto the target node and logged as its generation
    # history, so this is a canvas write on someone's sequence — and it spends
    # the shared provider key. With no node there is no project to authorize it
    # against, so only an unscoped caller may fire an unattached generation.
    with get_session() as s:
        if body.node_id is not None:
            resource_guard.authorize_node(s, user, body.node_id, "canvas.write")
        else:
            resource_guard.require_unscoped(s, user)
    try:
        result = await seed_audio.generate(
            prompt=body.prompt,
            audio_format=body.format,
            sample_rate=body.sample_rate,
            speech_rate=body.speech_rate,
            loudness_rate=body.loudness_rate,
            pitch_rate=body.pitch_rate,
            references=body.references,
            image_ref=body.image_ref,
            node_id=body.node_id,
        )
    except SeedAudioError as exc:
        logger.warning("seed-audio generate failed [%s]: %s", exc.code, exc)
        raise HTTPException(status_code=_STATUS_BY_CODE.get(exc.code, 502), detail=str(exc))
    return result
