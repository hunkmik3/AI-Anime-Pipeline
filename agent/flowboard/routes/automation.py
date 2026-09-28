"""Drama-film automation endpoints — the ``/automation`` demo surface.

``POST /api/automation/breakdown``  raw premise → cast, environments, shotlist
``POST /api/automation/prompt``     one cast/environment entry → house prompt
``POST /api/automation/plate``      prompt → generated image, as a data URL

Saved boards use durable database jobs and versioned production manifests.
Legacy unsaved-draft generation endpoints remain synchronous.
"""
from __future__ import annotations

import logging
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import AutomationProject, AutomationJob, AutomationRevision
from flowboard.routes.deps import get_optional_user
from flowboard.services import automation, auth, authored_contract, prompt_coverage, prompt_writer, automation_jobs, production_manifest, shot_package
from flowboard.services.llm.base import LLMError
from flowboard.services.video_analyzer.production import build_asset_prompt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/automation", tags=["automation"])


# ───────────────────────────── projects (CRUD) ─────────────────────────────
#
# A board is one row with one JSON blob. The pieces a list needs (name, title,
# when it was touched) are columns so the sidebar never loads a board it is not
# going to show.


def _owner_id(user: Any) -> Optional[uuid.UUID]:
    """The signed-in user's id, or None on the no-auth dev path."""
    raw = getattr(user, "id", None)
    return raw if isinstance(raw, uuid.UUID) else None


def _summary(p: AutomationProject) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "name": p.name,
        "title": p.title,
        "logline": p.logline,
        "runtime_seconds": p.runtime_seconds,
        "updated_at": p.updated_at.isoformat(),
        "revision": p.revision,
    }


def _full(p: AutomationProject) -> dict[str, Any]:
    return {**_summary(p), "script": p.script, "board": p.board or {}}


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectSave(BaseModel):
    """Every field optional: the board autosaves, and a rename should not have
    to send the whole graph back."""

    expected_revision: Optional[int] = Field(default=None, ge=0)
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    script: Optional[str] = None
    title: Optional[str] = None
    logline: Optional[str] = None
    runtime_seconds: Optional[int] = None
    board: Optional[dict[str, Any]] = None


def _load(session, project_id: uuid.UUID, user: Any) -> AutomationProject:
    row = session.get(AutomationProject, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Board không tồn tại.")
    owner = _owner_id(user)
    # Rows created before auth (owner NULL) stay open, the same way the rest of
    # the app treats its pre-auth rows.
    if owner and row.owner_user_id and row.owner_user_id != owner:
        raise HTTPException(status_code=404, detail="Board không tồn tại.")
    return row


@router.get("/projects")
def list_projects(user=Depends(get_optional_user)) -> list[dict[str, Any]]:
    owner = _owner_id(user)
    with get_session() as s:
        q = select(AutomationProject).order_by(AutomationProject.updated_at.desc())
        rows = list(s.exec(q))
        if owner:
            rows = [r for r in rows if r.owner_user_id in (None, owner)]
        return [_summary(r) for r in rows]


@router.post("/projects")
def create_project(body: ProjectCreate, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        row = AutomationProject(name=body.name.strip(), owner_user_id=_owner_id(user))
        s.add(row)
        s.commit()
        s.refresh(row)
        return _full(row)


@router.get("/projects/{project_id}")
def get_project(project_id: uuid.UUID, user=Depends(get_optional_user)) -> dict[str, Any]:
    with get_session() as s:
        row = _load(s, project_id, user)
        result = _full(row)
        result["board"] = automation_jobs.project_board(s, row)
        return result


# What a node holds that COST something to make. A save may replace any of these
# with a newer value; it may not blank one that is already there.
_EARNED = ("prompt", "clipUrl", "refs", "image", "referenceUrl", "mediaId")


def _keep_earned(incoming: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Let a save overwrite generated work, never silently delete it.

    The board page autosaves the WHOLE document from its own memory on any
    change, so a tab left open since before a batch ran will happily write its
    stale copy over sixteen freshly built prompts and three generated clips —
    which is exactly what it did, three times. A save that simply lacks a field
    is a stale save, not an instruction to throw the clip away; a save that
    carries a new value for it is a real edit and wins.
    """
    # Older board clients do not serialize authored-film metadata. Preserve
    # the explicit mode on those saves; omission is not a mode change.
    if "adaptation" not in incoming and "adaptation" in stored:
        incoming["adaptation"] = stored["adaptation"]
    nodes = incoming.get("nodes")
    if not isinstance(nodes, list) or not isinstance(stored.get("nodes"), list):
        return incoming
    was = {n.get("id"): (n.get("data") or {}) for n in stored["nodes"] if isinstance(n, dict)}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        old, new = was.get(node.get("id")), node.get("data")
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        for field in _EARNED:
            if old.get(field) and not new.get(field):
                new[field] = old[field]
        # A plate or a sheet is nested one level down; same rule applies.
        for holder in ("identity", "plate"):
            a, b = old.get(holder), new.get(holder)
            if isinstance(a, dict) and isinstance(b, dict):
                for field in _EARNED:
                    if a.get(field) and not b.get(field):
                        b[field] = a[field]
        # A node that got its clip back must not still claim to be waiting.
        if new.get("clipUrl") and new.get("status") in (None, "idle", "running"):
            new["status"] = "done"
    return incoming


@router.patch("/projects/{project_id}")
def save_project(
    project_id: uuid.UUID, body: ProjectSave, user=Depends(get_optional_user)
) -> dict[str, Any]:
    with get_session() as s:
        row = _load(s, project_id, user)
        # Lock before revision comparison, preventing concurrent lost updates.
        row = s.exec(select(AutomationProject).where(AutomationProject.id == project_id).with_for_update().execution_options(populate_existing=True)).one()
        if body.board is not None and body.expected_revision is None and row.revision > 0:
            raise HTTPException(428, detail="Board revision required. Reload before saving an older client.")
        if body.expected_revision is not None and body.expected_revision != row.revision:
            raise HTTPException(409, detail={"code":"board_conflict", "revision":row.revision,
                "message":"Board đã thay đổi ở phiên khác. Bản sửa của bạn được giữ tại máy; tải lại hoặc xuất trước khi lưu."})
        stored = automation_jobs.project_board(s, row)
        for field, value in body.model_dump(exclude_none=True, exclude={'expected_revision'}).items():
            if field == "board":
                value = _keep_earned(value, stored)
                # Results/status are server-owned, never reverted by stale autosave.
                value = automation_jobs.overlay(value, s.exec(select(AutomationJob).where(AutomationJob.project_id==project_id)).all())
            setattr(row, field, value)
        row.revision += 1
        row.updated_at = datetime.now(timezone.utc)
        s.add(row)
        manifest = production_manifest.build(row.board or {}, str(row.id))
        last = s.exec(select(AutomationRevision).where(AutomationRevision.project_id==row.id).order_by(AutomationRevision.revision.desc())).first()
        if not last or last.manifest.get('version') != manifest['version']:
            s.add(AutomationRevision(project_id=row.id, revision=row.revision, manifest=manifest))
        s.commit()
        s.refresh(row)
        return _summary(row)



@router.delete("/projects/{project_id}")
def delete_project(project_id: uuid.UUID, user=Depends(get_optional_user)) -> dict[str, bool]:
    with get_session() as s:
        row = _load(s, project_id, user)
        jobs = s.exec(select(AutomationJob).where(AutomationJob.project_id==project_id)).all()
        if any(j.status in automation_jobs.ACTIVE | {'unknown'} for j in jobs):
            raise HTTPException(409, detail="Resolve active jobs before deleting this board.")
        for j in jobs: s.delete(j)
        for r in s.exec(select(AutomationRevision).where(AutomationRevision.project_id==project_id)).all(): s.delete(r)
        s.flush()
        s.delete(row)
        s.commit()
    return {"ok": True}


@router.post("/projects/{project_id}/authored-contract")
def seal_authored_project(project_id: uuid.UUID, user=Depends(get_optional_user)) -> dict:
    """Seal saved creative intent without claiming source-video verification."""
    with get_session() as s:
        row = _load(s, project_id, user)
        try:
            report = authored_contract.seal(str(row.id), row.script or "", row.board or {})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row.board = {**row.board, "sourceVerification": report}
        row.revision += 1
        row.updated_at = datetime.now(timezone.utc)
        s.add(row)
        s.commit()
        return report


class BreakdownBody(BaseModel):
    script: str
    runtime_seconds: Optional[int] = Field(default=None, ge=15, le=3600)


class BreakdownResponse(BaseModel):
    title: str
    logline: str
    runtime_seconds: int
    characters: list[dict[str, Any]]
    environments: list[dict[str, Any]]
    sequences: list[dict[str, Any]]


@router.post("/breakdown", response_model=BreakdownResponse)
async def breakdown(body: BreakdownBody) -> BreakdownResponse:
    try:
        data = await automation.build_breakdown(
            body.script, runtime_seconds=body.runtime_seconds
        )
    except automation.AutomationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except LLMError as exc:
        # No provider pinned, or the CLI is missing — the user fixes this in
        # settings, so it is their problem to see, not a 500.
        raise HTTPException(status_code=503, detail=str(exc))
    return BreakdownResponse(**data)


class ShotsBody(BaseModel):
    sequence: dict[str, Any]
    characters: list[dict[str, Any]] = Field(default_factory=list)
    environments: list[dict[str, Any]] = Field(default_factory=list)
    # What the previous sequence left behind, and what the next one needs.
    # Without these the cut opens cold and the film reads as a slideshow.
    previous_exit: str = ""
    previous_label: str = ""
    next_summary: str = ""
    same_location: bool = False


class ShotsResponse(BaseModel):
    shots: list[dict[str, Any]]
    # What the sequence must establish, and its continuity locks. Both ride
    # into the video prompt's global block.
    function: list[str] = Field(default_factory=list)
    raccord: list[str] = Field(default_factory=list)
    exit_state: str = ""


@router.post("/shots", response_model=ShotsResponse)
async def shots(body: ShotsBody) -> ShotsResponse:
    """Cut one sequence into shots.

    One sequence per call, not the whole film: a full shotlist is a multi-minute
    generation that is thrown away the moment a character note changes, and it
    times out long before it finishes.

    The caller hands over the previous sequence's exit state, so a cut joins
    onto what came before instead of opening cold. Cut in running order for
    that to be worth anything.
    """
    try:
        cut = await automation.build_shots(
            body.sequence,
            characters=body.characters,
            environments=body.environments,
            previous_exit=body.previous_exit,
            previous_label=body.previous_label,
            next_summary=body.next_summary,
            same_location=body.same_location,
        )
    except automation.AutomationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return ShotsResponse(**cut)


class PromptBody(BaseModel):
    kind: str = Field(pattern="^(character|environment|keyframe|prop|background_group)$")
    asset: Optional[dict[str, Any]] = None
    dependency_references: list[dict[str, Any]] = Field(default_factory=list)
    character: Optional[dict[str, Any]] = None
    state: Optional[dict[str, Any]] = None
    environment: Optional[dict[str, Any]] = None
    # keyframe: the shot whose first or last frame this is, plus who is in it
    shot: Optional[dict[str, Any]] = None
    which: str = Field(default="start", pattern="^(start|end)$")
    characters: list[dict[str, Any]] = Field(default_factory=list)
    has_reference: bool = False
    aspect_ratio: str = "16:9"
    style: str = Field(default="realistic", pattern="^(realistic|anime|cg3d)$")


class PromptResponse(BaseModel):
    prompt: str


@router.post("/prompt", response_model=PromptResponse)
async def build_prompt(body: PromptBody) -> PromptResponse:
    """Assemble the house prompt for one entry.

    Separate from generation so the user can read and edit the prompt before
    spending a generation on it — the whole point of showing prompts on the
    nodes rather than hiding them behind the button. A character or place with
    a design brief then has its descriptive sections written by the prompt
    writer (GPT); the technical sections stay the house text.
    """
    if body.kind == "keyframe":
        if not body.shot:
            raise HTTPException(status_code=422, detail="A keyframe prompt needs a shot.")
        text = automation.build_keyframe_prompt(
            body.shot,
            which=body.which,
            characters=body.characters,
            environment=body.environment,
            style=body.style,
            aspect_ratio=body.aspect_ratio,
        )
    elif body.kind == "character":
        if not body.character or not body.state:
            raise HTTPException(status_code=422, detail="A character prompt needs a character and a state.")
        design = body.character.get("design") or None
        text = automation.build_character_prompt(
            body.character, body.state, has_reference=body.has_reference, style=body.style,
            design=design,
        )
        text, _ = await prompt_writer.write_image_prompt(
            text, kind="character", subject=str(body.character.get("name") or ""),
            design=design, style=body.style)
    elif body.kind in {"prop", "background_group"}:
        if not body.asset:
            raise HTTPException(status_code=422, detail="An asset prompt needs an asset.")
        text = build_asset_prompt({**body.asset, "dependency_references": body.dependency_references}, kind=body.kind, style=body.style,
                                 aspect_ratio=body.aspect_ratio, has_reference=body.has_reference)
    else:
        if not body.environment:
            raise HTTPException(status_code=422, detail="An environment prompt needs an environment.")
        design = body.environment.get("design") or None
        text = automation.build_environment_prompt(
            body.environment, aspect_ratio=body.aspect_ratio, style=body.style, design=design,
        )
        text, _ = await prompt_writer.write_image_prompt(
            text, kind="environment", subject=str(body.environment.get("name") or ""),
            design=design, style=body.style)
    return PromptResponse(prompt=text)


class PlateBody(BaseModel):
    prompt: str
    image_model: str = automation.DEFAULT_IMAGE_MODEL
    # 1K / 2K / 4K, capped to what the chosen model can actually deliver.
    image_size: str = automation.DEFAULT_IMAGE_SIZE
    aspect_ratio: str = "16:9"
    reference_urls: list[str] = Field(default_factory=list)
    variant_count: int = Field(default=1, ge=1, le=4)


class PlateImage(BaseModel):
    url: str
    reference_url: Optional[str] = None
    # Needed to build a KYC identity asset later; assets come from media rows,
    # never from URLs.
    media_id: Optional[str] = None
    persisted: bool = False


class PlateResponse(BaseModel):
    images: list[PlateImage]


@router.post("/plate", response_model=PlateResponse)
async def plate(body: PlateBody) -> PlateResponse:
    if body.image_model not in automation.IMAGE_MODELS:
        raise HTTPException(status_code=422, detail=f"Unknown image model {body.image_model!r}.")
    try:
        images = await automation.generate_plate(
            body.prompt,
            image_model=body.image_model,
            aspect_ratio=body.aspect_ratio,
            reference_urls=body.reference_urls,
            variant_count=body.variant_count,
            image_size=body.image_size,
        )
    except automation.AutomationError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return PlateResponse(images=[PlateImage(**img) for img in images])


class VideoPromptBody(BaseModel):
    sequence: dict[str, Any]
    shots: list[dict[str, Any]] = Field(default_factory=list)
    # Each carries ref_label / ref_url already assigned by the caller, because
    # @imageN binds by POSITION — the prompt and the reference list have to be
    # built from the same ordering or faces land on the wrong characters.
    characters: list[dict[str, Any]] = Field(default_factory=list)
    environment: Optional[dict[str, Any]] = None
    production_assets: Optional[list[dict[str, Any]]] = None
    reference_assets: list[dict[str, Any]] = Field(default_factory=list)
    source_verification: Optional[dict[str, Any]] = None
    style: str = Field(default="realistic", pattern="^(realistic|anime|cg3d)$")
    # English by default. Chinese staging rescued dialogue on a 30,000-character
    # prompt whose first line sat at 65% of its length; the section format now
    # used puts every line inside its own shot at ~5,000 characters, and in that
    # shape the user's English prompts delivered 8 of 8 lines. Translation stays
    # available, no longer assumed.
    language: str = Field(default="en", pattern="^(en|zh)$")
    # FORMAT line. Omitted when unknown rather than guessed.
    aspect_ratio: Optional[str] = None
    # The STYLE paragraph of the cast sheets these references were drawn with.
    # The writer describes the medium in their terms, so a clip of donghua
    # sheets is not told it is a generic 3D feature.
    style_note: str = ""


class VideoPromptResponse(BaseModel):
    prompt: str
    duration_seconds: int
    language: str = "en"


@router.post("/video/prompt", response_model=VideoPromptResponse)
async def build_video_prompt(body: VideoPromptBody) -> VideoPromptResponse:
    """Assemble a sequence's Seedance prompt, without spending a generation."""
    # Refused here, before anything is built or paid for, and named precisely so
    # the board can show which shot to rewrite rather than a bare failure.
    if body.production_assets is not None or body.source_verification is not None:
        raise HTTPException(status_code=422, detail="Dùng bước viết và kiểm tra prompt (/video/write) cho shotlist đã kiểm chứng.")
    unsafe = automation.unsafe_shots(body.shots, body.characters, body.environment)
    if unsafe:
        raise HTTPException(status_code=422, detail=(
            "Clip này có shot cởi/lộ đồ lót của nhân vật tuổi học sinh — không dựng: "
            + "; ".join(f"shot {n} (\"{words}\")" for n, words in unsafe)
            + ". Viết lại các shot đó rồi dựng lại."
        ))
    prompt = automation.build_video_prompt(
        body.sequence,
        body.shots,
        characters=body.characters,
        environment=body.environment,
        look=body.style,
        aspect_ratio=body.aspect_ratio,
    )
    # Cast and locations are named in the adaptation's language; translating
    # them is how the clip ended up speaking Chinese names over English lines.
    keep = [str(c.get("name") or "") for c in body.characters]
    keep += [str((body.environment or {}).get("name") or "")]
    translated = await automation.translate_prompt(prompt, body.language, keep=keep)
    # A failed translation falls back to English; say which one the board got
    # rather than let the label claim Chinese for an English prompt.
    return VideoPromptResponse(
        prompt=translated,
        duration_seconds=automation.clip_seconds(body.sequence, body.shots),
        language=body.language if translated != prompt else "en",
    )


class VideoWriteBody(VideoPromptBody):
    # Where the clip before this one ended — positions, props, mood — as the
    # writer described it then. Empty for the first clip, or when the clip
    # before was never written by the writer.
    previous_state: str = ""


class VideoWriteResponse(BaseModel):
    prompt: str
    duration_seconds: int
    end_state: str = ""
    writer: str = "template"
    warnings: list[str] = Field(default_factory=list)
    coverage: Optional[dict[str, Any]] = None
    contract_digest: str = ""
    coverage_token: str = ""


def _writing_digest(body: VideoWriteBody) -> str:
    return prompt_coverage.contract_digest(
        body.sequence, body.shots, body.characters, body.environment,
        body.production_assets, body.reference_assets, body.source_verification,
        look=body.style, aspect_ratio=body.aspect_ratio,
        previous_state=body.previous_state, style_note=body.style_note,
    )


def _coverage_token(body: VideoWriteBody, prompt: str, duration: int, coverage: Optional[dict]) -> str:
    """A server receipt for this exact reviewed prompt, inputs and coverage.

    The generation endpoint must not accept an arbitrary client-side `verified`
    flag. Domain separation keeps this receipt distinct from login tokens.
    """
    payload = json.dumps({"input": body.model_dump(), "prompt": prompt, "duration": duration,
                          "coverage": coverage}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hmac.new(auth._server_secret(), b"flowboard-prompt-coverage-v1\0" + payload.encode(), hashlib.sha256).hexdigest()


@router.post("/video/write", response_model=VideoWriteResponse)
async def write_video_prompt(body: VideoWriteBody) -> VideoWriteResponse:
    """Have the prompt writer (GPT) write a sequence's clip prompt, checked.

    Verified-source inputs require complete coverage and an independent review.
    Older freeform boards retain their existing template fallback.
    """
    strict = prompt_coverage.is_strict(body.production_assets, body.source_verification)
    if not strict and (body.production_assets is not None or body.source_verification is not None):
        # A legacy report travels with some boards; it is not a contract.
        body = body.model_copy(update={"production_assets": None, "source_verification": None,
                                       "reference_assets": []})
    unsafe = automation.unsafe_shots(body.shots, body.characters, body.environment)
    warnings: list[str] = []
    if prompt_writer.WRITER_ON:
        try:
            out = await prompt_writer.write_clip_prompt(
                body.sequence, body.shots, characters=body.characters, environment=body.environment,
                look=body.style, aspect_ratio=body.aspect_ratio, previous_state=body.previous_state,
                style_note=body.style_note, unsafe=unsafe,
                production_assets=body.production_assets, reference_assets=body.reference_assets,
                source_verification=body.source_verification,
            )
            return VideoWriteResponse(prompt=out.prompt, duration_seconds=out.duration,
                                      end_state=out.end_state, writer=out.model, warnings=out.warnings,
                                      coverage=out.coverage, contract_digest=out.contract_digest,
                                      coverage_token=_coverage_token(body, out.prompt, out.duration, out.coverage)
                                      if strict else "")
        except prompt_writer.WriterError as exc:
            logger.warning("video/write %s: %s", body.sequence.get("label"), exc)
            if strict:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            warnings.append(f"GPT chưa viết được prompt đạt kiểm tra ({exc}) — đang dùng template.")
    if strict:
        raise HTTPException(status_code=422, detail="Bật prompt writer để kiểm tra đầy đủ shot, nhân vật và đạo cụ trước khi gen.")
    if unsafe:
        raise HTTPException(status_code=422, detail=(
            "Clip này có shot cởi/lộ đồ lót của nhân vật tuổi học sinh — không dựng: "
            + "; ".join(f"shot {n} (\"{words}\")" for n, words in unsafe)
            + ". Viết lại các shot đó rồi dựng lại."
        ))
    prompt = automation.build_video_prompt(
        body.sequence, body.shots, characters=body.characters, environment=body.environment,
        look=body.style, aspect_ratio=body.aspect_ratio,
    )
    return VideoWriteResponse(prompt=prompt, duration_seconds=automation.clip_seconds(body.sequence, body.shots),
                              writer="template", warnings=warnings)


class VerifyPromptBody(BaseModel):
    prompt_contract: VideoWriteBody
    prompt: str = Field(min_length=1)
    end_state: str = ""


@router.post("/video/verify-prompt", response_model=VideoWriteResponse)
async def verify_video_prompt(body: VerifyPromptBody) -> VideoWriteResponse:
    """Check a provided draft unchanged and issue the normal signed receipt."""
    contract = body.prompt_contract
    if not prompt_writer.WRITER_ON:
        raise HTTPException(status_code=422, detail="Bật prompt writer để kiểm tra đầy đủ nội dung trước khi gen.")
    try:
        out = await prompt_writer.verify_provided_clip_prompt(
            body.prompt, contract.sequence, contract.shots,
            characters=contract.characters, environment=contract.environment,
            look=contract.style, aspect_ratio=contract.aspect_ratio,
            previous_state=contract.previous_state, style_note=contract.style_note, end_state=body.end_state,
            production_assets=contract.production_assets, reference_assets=contract.reference_assets,
            source_verification=contract.source_verification,
        )
    except prompt_writer.WriterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return VideoWriteResponse(prompt=out.prompt, duration_seconds=out.duration,
                              end_state=out.end_state, writer=out.model, warnings=out.warnings,
                              coverage=out.coverage, contract_digest=out.contract_digest,
                              coverage_token=_coverage_token(contract, out.prompt, out.duration, out.coverage))


class ClipBody(BaseModel):
    prompt: str
    # Order is the contract: index N here is what @image(N+1) refers to.
    reference_urls: list[str] = Field(default_factory=list)
    duration_seconds: int = Field(ge=automation.VIDEO_MIN_S, le=automation.VIDEO_MAX_S)
    aspect_ratio: str = "16:9"
    resolution: str = "720p"
    # Live-action drama: the cast plates are photorealistic people, which the
    # moderated endpoint refuses.
    unmoderated: bool = True
    # Media ids of EVERY reference, in @image1… order. Set to run the
    # person-driven path, which is what makes photoreal cast acceptable.
    kyc_media_ids: list[str] = Field(default_factory=list)
    # Keyframe interpolation: the clip starts on this image and ends on that
    # one. Sending a first frame switches the provider to i2v, where reference
    # images and KYC assets do not apply — see services/automation.generate_clip.
    first_frame_url: str = ""
    last_frame_url: str = ""
    # Continue from (or match) the clip before this one. Live-action's answer
    # to drift: a video of real people is accepted where a still is not.
    previous_clip_url: str = ""
    chain: str = Field(default="extend", pattern="^(extend|reference)$")
    prompt_contract: Optional[VideoWriteBody] = None
    coverage: Optional[dict[str, Any]] = None
    contract_digest: str = ""
    coverage_token: str = ""
    # The board this clip belongs to. Whether a receipt is required is read
    # from the saved board, never from whether the client chose to send one.
    project_id: Optional[uuid.UUID] = None
    sequence_key: str = ""


class ClipResponse(BaseModel):
    url: str
    persisted: bool
    job_id: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


def _board_is_strict(board: Any) -> bool:
    board = board if isinstance(board, dict) else {}
    return prompt_coverage.is_strict(board.get("productionAssets"), board.get("sourceVerification"))


def _board_refs(board: dict) -> set[str]:
    """Every reference URL and media id a saved board's sheets and plates carry."""
    out: set[str] = set()

    def take(plate: Any) -> None:
        if isinstance(plate, dict):
            out.update(v for v in (plate.get("referenceUrl"), plate.get("mediaId")) if v)

    for node in board.get("nodes") or []:
        data = (node or {}).get("data") or {}
        take(data.get("identity"))
        take(data.get("plate"))
        for plate in (data.get("states") or {}).values():
            take(plate)
    return out


_STRICT_REFS: dict[str, Any] = {"stamp": None, "refs": set()}


def _strict_refs() -> set[str]:
    """References belonging to strict boards, recomputed when any board changes."""
    with get_session() as s:
        rows = s.exec(select(AutomationProject.id, AutomationProject.updated_at)).all()
        stamp = tuple(sorted((str(i), str(u)) for i, u in rows))
        if stamp != _STRICT_REFS["stamp"]:
            refs: set[str] = set()
            for row in s.exec(select(AutomationProject)).all():
                if _board_is_strict(row.board):
                    refs |= _board_refs(row.board or {})
            _STRICT_REFS.update(stamp=stamp, refs=refs)
    return _STRICT_REFS["refs"]


def _requires_contract(body: "ClipBody") -> bool:
    """Server-side answer to "must this generation carry a coverage receipt?".

    The saved board decides when the request names one. A request that names
    none — a tab from before this contract, a script, a hand-made API call —
    is still held to it when any of its references belongs to a strict board.
    """
    if body.project_id:
        with get_session() as s:
            row = s.get(AutomationProject, body.project_id)
            if row is not None:
                return _board_is_strict(row.board)
    wanted = {*body.reference_urls, *body.kyc_media_ids} - {""}
    return bool(wanted & _strict_refs())


def _validate_generation_contract(body: ClipBody) -> None:
    contract = body.prompt_contract
    if contract and contract.sequence.get('shot_package'):
        with get_session() as s:
            project = s.get(AutomationProject, body.project_id) if body.project_id else None
            if project is None:
                raise HTTPException(422, detail="Prepared shots require their saved project.")
            try:
                automation_jobs.validate_package(automation_jobs.project_board(s, project), str(project.id), contract)
            except ValueError as exc:
                raise HTTPException(422, detail=str(exc)) from exc
    strict = contract is not None and prompt_coverage.is_strict(contract.production_assets,
                                                                contract.source_verification)
    if not strict and _requires_contract(body):
        raise HTTPException(status_code=422, detail=(
            "Board này dùng video gốc đã đối chiếu: cần Agent 2 viết và kiểm tra prompt "
            "(kèm biên nhận) trước khi gen."))
    if contract is None:
        if body.contract_digest or body.coverage or body.coverage_token:
            raise HTTPException(status_code=422, detail="Thiếu dữ liệu shot và tài sản đã dùng để kiểm tra prompt.")
        return  # Existing freeform/script boards have no source-video contract.
    if not strict:
        return
    issues = prompt_coverage.verify_prompt_contract(body.prompt, body.coverage,
        expected_digest=body.contract_digest, actual_digest=_writing_digest(contract))
    if (contract.source_verification or {}).get("method") == "authored_script":
        with get_session() as s:
            row = s.get(AutomationProject, body.project_id) if body.project_id else None
            if row is None:
                issues.append("Authored generation requires its saved project.")
            else:
                issues.extend(authored_contract.current_project_issues(str(row.id), body.sequence_key,
                    row.board or {}, row.script or "", contract.source_verification, contract.shots))
    expected_token = _coverage_token(contract, body.prompt, body.duration_seconds, body.coverage)
    if not body.coverage_token or not hmac.compare_digest(expected_token, body.coverage_token):
        issues.append("Prompt hoặc dữ liệu đã đổi sau khi kiểm tra. Viết và kiểm tra lại prompt.")
    references = [*contract.characters, *([contract.environment] if contract.environment else []),
                  *contract.reference_assets]
    issues.extend(prompt_coverage.validate_source_contract(contract.shots, contract.production_assets or [],
                                                          contract.source_verification, references))
    # Every asset keeps its binding, but a shared atlas is sent only once.
    # The source validator above rejects conflicting or duplicate bindings.
    ordered, reference_issues = prompt_coverage.reference_slots(references)
    issues.extend(reference_issues)
    if [r.get("ref_url") for r in ordered] != body.reference_urls:
        issues.append("Ảnh gửi gen khác thứ tự/nội dung ảnh đã dùng để kiểm tra prompt.")
    if body.kyc_media_ids and body.kyc_media_ids != [r.get("media_id") for r in ordered]:
        issues.append("Media ID gửi gen không khớp ảnh tham chiếu đã kiểm tra.")
    if contract.aspect_ratio and body.aspect_ratio != contract.aspect_ratio:
        issues.append("Tỉ lệ khung hình đã đổi; viết lại prompt trước khi gen.")
    if body.first_frame_url or body.last_frame_url or body.previous_clip_url:
        issues.append("Prompt này được kiểm tra với bộ ảnh @image. Chọn chế độ ảnh tham chiếu để giữ đầy đủ tài sản.")
    if issues:
        raise HTTPException(status_code=422, detail="\n".join(dict.fromkeys(issues)))


@router.post("/video/clip", response_model=ClipResponse)
async def clip(body: ClipBody) -> ClipResponse:
    _validate_generation_contract(body)
    try:
        out = await automation.generate_clip(
            body.prompt,
            reference_urls=body.reference_urls,
            duration_seconds=body.duration_seconds,
            aspect_ratio=body.aspect_ratio,
            resolution=body.resolution,
            unmoderated=body.unmoderated,
            kyc_media_ids=body.kyc_media_ids,
            first_frame_url=body.first_frame_url,
            last_frame_url=body.last_frame_url,
            previous_clip_url=body.previous_clip_url,
            chain=body.chain,
        )
    except automation.AutomationError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return ClipResponse(**out)


class ClipEntry(BaseModel):
    url: str
    label: str = ""
    title: str = ""


class BundleBody(BaseModel):
    # Order is the contract: entry N becomes file NN_… so the zip sorts into
    # story order. The board sends them in running order; this preserves it.
    clips: list[ClipEntry] = Field(default_factory=list, max_length=200)
    filename: str = "clips.zip"


@router.post("/export/clips")
async def export_clips(body: BundleBody, background: BackgroundTasks):
    """Zip every generated clip, numbered so it sorts into story order."""
    try:
        path = await automation.bundle_clips([c.model_dump() for c in body.clips])
    except automation.AutomationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # Delete after the response is flushed — the file is hundreds of MB and
    # nothing else will ever come looking for it.
    background.add_task(lambda: path.unlink(missing_ok=True))
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", body.filename) or "clips.zip"
    return FileResponse(path, media_type="application/zip", filename=name, background=background)


class PlateEntry(BaseModel):
    url: str
    name: str = ""
    kind: str = Field(default="character", pattern="^(character|environment|prop|background_group)$")


class PlateBundleBody(BaseModel):
    plates: list[PlateEntry] = Field(default_factory=list, max_length=300)
    filename: str = "tao-hinh.zip"


@router.post("/export/plates")
async def export_plates(body: PlateBundleBody, background: BackgroundTasks):
    """Zip every sheet and plate, each file named after its own subject."""
    try:
        path = await automation.bundle_plates([p.model_dump() for p in body.plates])
    except automation.AutomationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    background.add_task(lambda: path.unlink(missing_ok=True))
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", body.filename) or "tao-hinh.zip"
    return FileResponse(path, media_type="application/zip", filename=name, background=background)


class IngestBody(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=40)


class IngestResponse(BaseModel):
    media_ids: dict[str, Optional[str]]


@router.post("/ingest", response_model=IngestResponse)
async def ingest(body: IngestBody) -> IngestResponse:
    """Backfill media rows for plates that only exist as published URLs."""
    return IngestResponse(media_ids=await automation.ingest_published(body.urls))


class CapabilitiesResponse(BaseModel):
    image_models: list[str]
    default_image_model: str
    # model id → the largest resolution it really delivers ("2K" / "4K")
    image_model_max: dict[str, str]
    image_sizes: list[str]
    default_image_size: str
    atrium_configured: bool
    # Seedream runs on Avis, not Atrium, so a board can still generate with one
    # engine configured and not the other.
    avis_configured: bool
    reference_chain: bool


@router.get("/capabilities", response_model=CapabilitiesResponse)
def capabilities() -> CapabilitiesResponse:
    """What this install can actually do, so the page says so up front.

    Without Atrium there is no image generation at all; without R2 there is
    generation but no identity chain, which is worth knowing BEFORE spending
    eight sheets discovering the faces do not match.
    """
    from flowboard.services.flowstudio import atrium_api, avis_api

    return CapabilitiesResponse(
        image_models=list(automation.IMAGE_MODELS),
        default_image_model=automation.DEFAULT_IMAGE_MODEL,
        image_model_max=dict(automation.IMAGE_MODEL_MAX),
        image_sizes=list(automation.IMAGE_SIZES),
        default_image_size=automation.DEFAULT_IMAGE_SIZE,
        atrium_configured=atrium_api.is_configured(),
        avis_configured=avis_api.is_configured(),
        reference_chain=automation.reference_chain_available(),
    )


class JobCreate(BaseModel):
    kind: str = Field(pattern="^(clip|plate|write)$")
    node_id: str
    slot: str = ""
    payload: dict[str, Any]
    request_key: str = Field(min_length=1, max_length=180)
    expected_revision: int = Field(ge=0)
    input_fingerprint: str = Field(default="", max_length=200)


@router.post("/projects/{project_id}/jobs", status_code=202)
def create_job(project_id: uuid.UUID, body: JobCreate, user=Depends(get_optional_user)):
    with get_session() as s:
        row = _load(s, project_id, user)
        if row.revision != body.expected_revision:
            raise HTTPException(409, detail="Board changed; save/reload before submitting a job.")
        board = automation_jobs.project_board(s,row)
        node = next((n.get('data') for n in board.get('nodes',[]) if n.get('id')==body.node_id),None)
        if not node: raise HTTPException(422,detail="Job target is not on this board.")
        metadata={}
        if body.kind=='clip':
            try: clip_body=ClipBody.model_validate(body.payload)
            except ValidationError as exc: raise HTTPException(422,detail=str(exc)) from exc
            if clip_body.project_id != project_id or body.node_id != 'vid:'+clip_body.sequence_key or node.get('kind')!='video':
                raise HTTPException(422,detail="Clip/project/target mismatch.")
            _validate_generation_contract(clip_body)
            seq = next((n['data'] for n in board.get('nodes',[]) if n.get('id')=='seq:'+clip_body.sequence_key),None)
            if not seq:raise HTTPException(422,detail="Clip has no saved shotlist.")
            if clip_body.prompt_contract:
                if clip_body.prompt_contract.shots != seq.get('shots',[]):
                    raise HTTPException(409,detail="Shotlist changed since this prompt was verified.")
                supplied=clip_body.prompt_contract.sequence.get('production_context')
                current=production_manifest.context(production_manifest.build(board,str(project_id)),clip_body.sequence_key)
                if supplied and supplied.get('version')!=current['version']:
                    raise HTTPException(409,detail="Scene state or asset version changed. Rewrite this prompt.")
            payload=clip_body.model_dump(mode='json')
        elif body.kind=='write':
            try: write_body=VideoWriteBody.model_validate(body.payload)
            except ValidationError as exc: raise HTTPException(422,detail=str(exc)) from exc
            if node.get('kind')!='video' or body.node_id!='vid:'+str(write_body.sequence.get('key','')):
                raise HTTPException(422,detail="Writer target mismatch.")
            try:automation_jobs.validate_writing(project_id,write_body)
            except ValueError as exc:raise HTTPException(409,detail=str(exc)) from exc
            payload=write_body.model_dump(mode='json')
            metadata={'base_prompt':node.get('prompt',''),'input_fingerprint':body.input_fingerprint}
        else:
            if node.get('kind')=='character':
                if body.slot!='identity' and body.slot not in node.get('states',{}):raise HTTPException(422,detail="Unknown character state.")
            elif node.get('kind') not in ('environment','asset','video') or body.slot not in ('plate','startFrame','endFrame'):
                raise HTTPException(422,detail="Invalid plate target.")
            try: plate_body=PlateBody.model_validate(body.payload)
            except ValidationError as exc: raise HTTPException(422,detail=str(exc)) from exc
            if plate_body.image_model not in automation.IMAGE_MODELS:raise HTTPException(422,detail="Unknown image model.")
            payload=plate_body.model_dump(mode='json')
    try:return automation_jobs.enqueue(project_id,body.kind,body.node_id,body.slot,payload,body.request_key,body.expected_revision,metadata)
    except ValueError as exc:raise HTTPException(409,detail=str(exc)) from exc


@router.get("/projects/{project_id}/jobs")
def list_jobs(project_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        _load(s,project_id,user)
        return [automation_jobs.public(j) for j in s.exec(select(AutomationJob).where(AutomationJob.project_id==project_id).order_by(AutomationJob.created_at)).all()]


@router.get("/projects/{project_id}/jobs/{job_id}")
def get_job(project_id: uuid.UUID, job_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        _load(s,project_id,user);j=s.get(AutomationJob,job_id)
        if not j or j.project_id!=project_id:raise HTTPException(404,detail="Job not found.")
        return automation_jobs.public(j)


class ReconcileJob(BaseModel):
    provider_job_id: str = ""


@router.post("/projects/{project_id}/jobs/{job_id}/resume")
def resume_job(project_id: uuid.UUID, job_id: uuid.UUID, body: ReconcileJob, user=Depends(get_optional_user)):
    with get_session() as s:
        _load(s,project_id,user)
        j=s.exec(select(AutomationJob).where(AutomationJob.id==job_id).with_for_update()).first()
        if not j or j.project_id!=project_id:raise HTTPException(404,detail="Job not found.")
        if j.status!='unknown' or j.kind!='clip':raise HTTPException(409,detail="Only unresolved video jobs can resume polling.")
        if j.provider_job_id and body.provider_job_id and j.provider_job_id!=body.provider_job_id:
            raise HTTPException(409,detail="Cannot replace an existing provider job ID.")
        provider_id=j.provider_job_id or body.provider_job_id.strip()
        if not provider_id or not re.fullmatch(r'(b2b:)?[A-Za-z0-9_-]+',provider_id):raise HTTPException(422,detail="A valid provider receipt ID is required; this does not submit a new generation.")
        j.provider_job_id=provider_id;j.prepared={**j.prepared,'external_job_id':provider_id}
        j.status='running';j.lease_token='';j.lease_until=None;j.error='';s.add(j);s.commit();return automation_jobs.public(j)


@router.post("/projects/{project_id}/jobs/{job_id}/cancel")
def cancel_job(project_id: uuid.UUID, job_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        _load(s,project_id,user);j=s.exec(select(AutomationJob).where(AutomationJob.id==job_id).with_for_update()).first()
        if not j or j.project_id!=project_id:raise HTTPException(404,detail="Job not found.")
        if j.status!='queued':raise HTTPException(409,detail="Only jobs not yet dispatched can be cancelled locally.")
        j.status='cancelled';j.updated_at=datetime.now(timezone.utc);s.add(j);s.commit();return automation_jobs.public(j)


@router.get("/projects/{project_id}/production")
def production_state(project_id: uuid.UUID, sequence_key: str = "", user=Depends(get_optional_user)):
    with get_session() as s:
        row=_load(s,project_id,user)
        manifest=production_manifest.build(automation_jobs.project_board(s,row),str(project_id))
        return production_manifest.context(manifest,sequence_key) if sequence_key else manifest


@router.get("/projects/{project_id}/revisions")
def production_revisions(project_id: uuid.UUID, user=Depends(get_optional_user)):
    with get_session() as s:
        _load(s,project_id,user)
        return [{'revision':r.revision,'created_at':r.created_at,'version':r.manifest.get('version')} for r in s.exec(select(AutomationRevision).where(AutomationRevision.project_id==project_id).order_by(AutomationRevision.revision.desc())).all()]


@router.get("/projects/{project_id}/revisions/{revision}")
def production_revision(project_id: uuid.UUID, revision:int, user=Depends(get_optional_user)):
    with get_session() as s:
        _load(s,project_id,user);r=s.exec(select(AutomationRevision).where(AutomationRevision.project_id==project_id,AutomationRevision.revision==revision)).first()
        if not r:raise HTTPException(404,detail="Revision not found.")
        return r.manifest


class ResolveAbsentJob(BaseModel):
    provider_checked_no_submission: bool
    note: str = Field(min_length=8,max_length=1000)


@router.post("/projects/{project_id}/jobs/{job_id}/resolve-absent")
def resolve_absent_job(project_id: uuid.UUID, job_id: uuid.UUID, body: ResolveAbsentJob, user=Depends(get_optional_user)):
    with get_session() as s:
        _load(s,project_id,user)
        j=s.exec(select(AutomationJob).where(AutomationJob.id==job_id).with_for_update()).first()
        if not j or j.project_id!=project_id:raise HTTPException(404,detail="Job not found.")
        if j.status!='unknown' or j.provider_job_id or not body.provider_checked_no_submission:
            raise HTTPException(409,detail="Requires a provider check confirming no submission; known provider IDs must be polled.")
        j.status='failed';j.error='Manual provider reconciliation: '+body.note
        j.prepared={**j.prepared,'reconciliation':{'outcome':'not_submitted','note':body.note,'at':datetime.now(timezone.utc).isoformat(),'by':str(_owner_id(user) or 'local')}}
        j.updated_at=datetime.now(timezone.utc);s.add(j);s.commit();return automation_jobs.public(j)


@router.get("/projects/{project_id}/shot-packages")
def shot_packages(project_id: uuid.UUID, sequence_key: str = "", limit: int = 5, user=Depends(get_optional_user)):
    with get_session() as s:
        row = _load(s, project_id, user)
        board = automation_jobs.project_board(s, row)
        try:
            result = shot_package.build(board, str(project_id), sequence_key)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return result if sequence_key else result[:max(1, min(50, limit))]


@router.get("/projects/{project_id}/shot-keyframe")
def shot_keyframe_prompt(project_id: uuid.UUID, sequence_key: str, shot_index: int, which: str = "start", user=Depends(get_optional_user)):
    with get_session() as s:
        row = _load(s, project_id, user)
        try:
            package = shot_package.build(automation_jobs.project_board(s, row), str(project_id), sequence_key)
            return shot_package.keyframe(package, shot_index, which)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc


class ShotFrameRequest(BaseModel):
    sequence_key: str
    shot_index: int = Field(ge=0)
    which: str = Field(default="start", pattern="^(start|end)$")
    expected_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=180)


@router.post("/projects/{project_id}/shot-keyframe", status_code=202)
def queue_shot_keyframe(project_id: uuid.UUID, body: ShotFrameRequest, user=Depends(get_optional_user)):
    with get_session() as s:
        row = _load(s, project_id, user)
        board = automation_jobs.project_board(s, row)
        if not any(n.get('id') == 'vid:' + body.sequence_key for n in board.get('nodes', [])):
            raise HTTPException(422, detail="Clip has no video node.")
        try:
            package = shot_package.build(board, str(project_id), body.sequence_key)
            frame = shot_package.keyframe(package, body.shot_index, body.which)
            # Refuse invalid target adaptations and missing state sheets before a paid image.
            shot_id = frame['shot_id']
            blockers = [i for i in package['issues'] if i.get('blocking') and i.get('shot', shot_id) == shot_id]
            if blockers: raise ValueError('; '.join(i['code'] for i in blockers))
            payload = PlateBody(prompt=frame['prompt'], reference_urls=frame['reference_urls'],
                                aspect_ratio=frame['aspect_ratio'], image_model=board.get('imageModel', automation.DEFAULT_IMAGE_MODEL),
                                image_size=board.get('imageSize', '2K')).model_dump(mode='json')
            if payload['image_model'] not in automation.IMAGE_MODELS: raise ValueError('Unknown image model')
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
    metadata = {'shot_frame': {k: frame[k] for k in ('package_version','shot_id','shot_index','which')},
                'sequence_key': body.sequence_key}
    try:
        return automation_jobs.enqueue(project_id, 'plate', 'vid:' + body.sequence_key,
            f'shotframe:{body.shot_index}:{body.which}', payload, body.request_key, body.expected_revision, metadata)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


class RaccordRequest(BaseModel):
    sequence_key: str = ""
    expected_revision: int = Field(ge=0)


@router.post("/projects/{project_id}/raccord", status_code=202)
def prepare_raccord(project_id: uuid.UUID, body: RaccordRequest, user=Depends(get_optional_user)):
    """Queue/cache whole-scene plans, without any approval or video-output QA."""
    from flowboard.services import raccord
    with get_session() as s:
        project = _load(s, project_id, user)
        if project.revision != body.expected_revision:
            raise HTTPException(409, detail="Board changed before scene planning; save and retry.")
        board = automation_jobs.project_board(s, project)
        try:
            scenes = raccord.scene_inputs(board, str(project_id))
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
    if body.sequence_key:
        scenes = {key: scene for key,scene in scenes.items()
                  if any(shot['sequence_key'] == body.sequence_key for shot in scene['shots'])}
        if not scenes: raise HTTPException(422, detail="Clip has no scene shots.")
    queued = []
    for scene in scenes.values():
        try:
            target = 'raccord:' + scene['version']
            with get_session() as session:
                previous = session.exec(select(AutomationJob).where(AutomationJob.project_id == project_id,
                    AutomationJob.kind == 'raccord', AutomationJob.node_id == target)
                    .order_by(AutomationJob.created_at.desc())).first()
                if previous and previous.status in automation_jobs.ACTIVE | {'succeeded'}:
                    queued.append(automation_jobs.public(previous))
                    continue
                request_key = target + (':' + str(previous.id) if previous else '')
            queued.append(automation_jobs.enqueue(project_id, 'raccord', target, '',
                          scene, request_key, body.expected_revision))
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
    return queued


class ProductionRunConfig(BaseModel):
    mode: str = Field(default='prepare', pattern='^(prepare|render)$')
    sequence_keys: list[str] = Field(default_factory=list, max_length=2000)
    boundary_frames: bool = False
    assemble: bool = True
    fps: int = Field(default=24, ge=24, le=60)
    resolution: str = Field(default='720p', pattern='^(720p|1080p)$')
    image_parallel: int = Field(default=4, ge=1, le=32)
    video_parallel: int = Field(default=4, ge=1, le=32)
    text_parallel: int = Field(default=8, ge=1, le=64)
    max_images: int = Field(default=100, ge=0, le=10000)
    max_videos: int = Field(default=100, ge=0, le=10000)
    max_text: int = Field(default=500, ge=0, le=10000)


class ProductionRunRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=180)
    config: ProductionRunConfig = Field(default_factory=ProductionRunConfig)


@router.post('/projects/{project_id}/production-runs/preview')
def preview_production_run(project_id: uuid.UUID, body: ProductionRunConfig, user=Depends(get_optional_user)):
    from flowboard.services import production_run
    with get_session() as s:
        project=_load(s,project_id,user)
        try:return production_run.preview(automation_jobs.project_board(s,project),project_id,body.model_dump())
        except ValueError as exc:raise HTTPException(422,detail=str(exc)) from exc


@router.post('/projects/{project_id}/production-runs',status_code=202)
def start_production_run(project_id: uuid.UUID, body: ProductionRunRequest, user=Depends(get_optional_user)):
    from flowboard.services import production_run
    with get_session() as s:_load(s,project_id,user)
    try:return production_run.start(project_id,body.expected_revision,body.config.model_dump(),body.request_key)
    except ValueError as exc:raise HTTPException(409,detail=str(exc)) from exc


@router.post('/projects/{project_id}/production-runs/{run_id}/{action}')
def control_production_run(project_id: uuid.UUID, run_id: uuid.UUID, action: str, user=Depends(get_optional_user)):
    from flowboard.services import production_run
    if action not in ('pause','resume'):raise HTTPException(404,detail='Unknown action')
    with get_session() as s:
        _load(s,project_id,user)
        s.exec(select(AutomationProject).where(AutomationProject.id==project_id).with_for_update()).one()
        run=s.exec(select(AutomationJob).where(AutomationJob.id==run_id).with_for_update()).first()
        if not run or run.project_id!=project_id or run.kind!=production_run.KIND:raise HTTPException(404,detail='Run not found')
        if action=='pause' and run.status!='running':raise HTTPException(409,detail='Run is not active')
        if action=='resume' and run.status not in ('paused','blocked'):raise HTTPException(409,detail='Run is not paused or blocked')
        if action=='resume':
            active=s.exec(select(AutomationJob).where(AutomationJob.project_id==project_id,AutomationJob.kind==production_run.KIND,AutomationJob.status=='running')).first()
            if active:raise HTTPException(409,detail='Another production run is active')
        run.status='paused' if action=='pause' else 'running';run.error='';run.result={**run.result,'errors':[]}
        run.updated_at=automation_jobs.now();s.add(run);s.commit();return automation_jobs.public(run)


@router.get('/projects/{project_id}/production-runs/{run_id}/film')
def download_production_film(project_id: uuid.UUID, run_id: uuid.UUID, user=Depends(get_optional_user)):
    from flowboard.config import STORAGE_DIR
    from pathlib import Path
    with get_session() as s:
        _load(s,project_id,user);run=s.get(AutomationJob,run_id)
        if not run or run.project_id!=project_id or run.kind!='production_run' or run.status!='succeeded':
            raise HTTPException(404,detail='Completed film not found')
        filename=run.result.get('output',{}).get('filename','')
        if not filename or Path(filename).name!=filename:raise HTTPException(404,detail='Film not found')
        path=Path(STORAGE_DIR)/'production-renders'/filename
        if not path.is_file():raise HTTPException(404,detail='Rendered file no longer exists')
        return FileResponse(path,media_type='video/mp4',filename='film.mp4')
