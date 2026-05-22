"""Auto-prompt routes.

`POST /api/prompt/auto { node_id }` — Claude-composed prompt built
from the immediate-upstream context + the project/scene bible auto-
injection (Phase 6).

`POST /api/prompt/auto-batch { node_id, count }` — N variant-distinct
prompts in one LLM call.

`POST /api/prompt/parse-script { scene_id, script_text }` — break a
Vietnamese-or-any-language scene script into structured shot
breakdowns (Phase 6.4). Used by the SceneView ScriptInputDialog.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from flowboard.services import prompt_synth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/prompt", tags=["prompt"])


class AutoPromptBody(BaseModel):
    node_id: int
    # Optional video-only constraint: e.g. "static" → synth uses the camera-
    # locked system prompt and avoids dolly/zoom suggestions.
    camera: Optional[str] = None


class AutoPromptResponse(BaseModel):
    node_id: int
    prompt: str


@router.post("/auto", response_model=AutoPromptResponse)
async def auto_prompt(body: AutoPromptBody) -> AutoPromptResponse:
    try:
        text = await prompt_synth.auto_prompt(body.node_id, camera=body.camera)
    except prompt_synth.PromptSynthError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return AutoPromptResponse(node_id=body.node_id, prompt=text)


class AutoPromptBatchBody(BaseModel):
    node_id: int
    count: int
    camera: Optional[str] = None


class AutoPromptBatchResponse(BaseModel):
    node_id: int
    prompts: list[str]


@router.post("/auto-batch", response_model=AutoPromptBatchResponse)
async def auto_prompt_batch(body: AutoPromptBatchBody) -> AutoPromptBatchResponse:
    """Return N pose-distinct prompts so that an N-variant image gen
    actually produces N different shots instead of N seeds of the same
    stance."""
    if body.count < 1 or body.count > 8:
        raise HTTPException(status_code=400, detail="count must be 1..8")
    try:
        prompts = await prompt_synth.auto_prompt_batch(
            body.node_id, body.count, camera=body.camera
        )
    except prompt_synth.PromptSynthError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return AutoPromptBatchResponse(node_id=body.node_id, prompts=prompts)


class ParseScriptBody(BaseModel):
    scene_id: uuid.UUID
    script_text: str


class ParseScriptShot(BaseModel):
    order: int
    script_text: str
    camera_angle: str
    characters_in_frame: list[str]
    environment: str
    dialogue: Optional[str] = None
    beat_notes: str


class ParseScriptResponse(BaseModel):
    scene_id: uuid.UUID
    shots: list[ParseScriptShot]


@router.post("/parse-script", response_model=ParseScriptResponse)
async def parse_script(body: ParseScriptBody) -> ParseScriptResponse:
    """Break a (Vietnamese-or-any-language) scene script into discrete
    cinematic shots.

    The LLM is instructed to preserve ``script_text`` verbatim in the
    source language while emitting English meta fields. Used by the
    SceneView ScriptInputDialog to bulk-create shots with parsed
    camera / character / environment hints already populated.
    """
    if not body.script_text.strip():
        raise HTTPException(status_code=400, detail="script_text is empty")
    try:
        shots = await prompt_synth.parse_script(body.scene_id, body.script_text)
    except prompt_synth.PromptSynthError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return ParseScriptResponse(
        scene_id=body.scene_id,
        shots=[ParseScriptShot(**s) for s in shots],
    )
