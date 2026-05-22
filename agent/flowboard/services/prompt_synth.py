"""Auto-prompt synthesizer — anime narrative pipeline (Phase 6).

Given a target node, walks the immediate-upstream graph, collects
upstream brief / script / bible / master-shot context, looks up the
Project Bible + Scene Bible from the shot's parent hierarchy, and
asks the configured Auto-Prompt provider to compose a single image-
or motion-generation prompt that combines them.

Provider routing goes through ``run_llm("auto_prompt", ...)``. User
picks which one in Settings → AI Providers; default is Claude.

Bilingual: upstream context may be Vietnamese. The system prompts
instruct the LLM to read source-language natively and emit the
generation prompt in English (image / video providers expect EN).
The script-parse endpoint preserves ``script_text`` verbatim in the
source language while emitting meta fields (camera, environment,
beat notes) in English.

The legacy fashion-editorial system prompts are preserved verbatim
in ``prompt_synth_legacy.py`` for reference; nothing in this module
imports from there.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Asset, Edge, Node, Project, Scene, Shot
from flowboard.services.activity import record_activity
from flowboard.services.llm import run_llm
from flowboard.services.llm.base import LLMError

logger = logging.getLogger(__name__)


# ── System prompts: still image (anime cel) ──────────────────────────────


_ANIME_IMAGE_SYSTEM = (
    "You are an image-generation prompt builder for an anime narrative "
    "production pipeline. Output ONE concise prompt (max 380 chars) for "
    "a single cinematic anime frame combining the upstream context.\n\n"
    "STYLE FLOOR (always apply unless Project Bible overrides):\n"
    "  • 2D anime, cel-shaded, hand-drawn aesthetic — clean line art "
    "with deliberate weight variation, flat color fills with selective "
    "rim/key shading. NOT 3D-rendered, NOT photoreal, NOT AI-smooth "
    "plastic-skin look.\n"
    "  • Cinematic framing — pick ONE explicit camera angle from the "
    "vocabulary: wide establishing, medium two-shot, medium close-up, "
    "close-up, extreme close-up, low-angle hero, high-angle bird's eye, "
    "over-the-shoulder, Dutch tilt. Match the angle to the dramatic "
    "beat described in the script.\n"
    "  • Rule of thirds composition, intentional negative space, "
    "layered depth (foreground / midground / background plates).\n"
    "  • Anime lighting language — directional key light with crisp "
    "cel shadows, ambient bounce, optional rim light when the beat "
    "calls for it. No global illumination softness — cel anime has "
    "hard shadow boundaries.\n\n"
    "IDENTITY PRESERVATION (when character refs are upstream):\n"
    "  • A character ref carries the canonical face, hair, eye color, "
    "and silhouette. Reproduce these faithfully across shots — wardrobe "
    "and expression may change, but identity markers must NOT drift.\n"
    "  • Reference each character by its `ref_image_N` label so "
    "downstream bind-by-position works.\n\n"
    "ESTABLISHING SHOT (when `establishing_shot_ref` is upstream):\n"
    "  • The `establishing_shot_ref` is the scene's master shot — it "
    "fixes the spatial layout, lighting, and color palette for every "
    "shot in this scene. Match composition language, lighting "
    "direction, and palette to it. The new frame may zoom in, change "
    "angle, or reframe — but the world must read as the SAME location "
    "at the SAME moment.\n\n"
    "ANTI-PATTERNS (negative direction — always include):\n"
    "  • No photoreal skin texture, no 3D-rendered look, no plastic "
    "shine.\n"
    "  • No motion blur (this is a still — sharp lineart only).\n"
    "  • No text overlays, no logos, no signage in any language.\n"
    "  • No off-model proportions — match canonical anime body ratios.\n\n"
    "BILINGUAL: upstream context may be Vietnamese. Read it natively, "
    "but OUTPUT the final prompt in English (downstream image providers "
    "expect English). Do not translate the user's intent; carry "
    "semantic meaning.\n\n"
    "Output the prompt only — no preamble, no explanation."
)


_ANIME_IMAGE_MULTI_SUBJECT_CLAUSE = (
    "\n\nMULTI-SUBJECT MODE: This frame contains 2+ characters. Compose "
    "as a single anime group composition where every character is "
    "visible and on-model:\n"
    "  • Reference each character by their `ref_image_N` label so the "
    "downstream provider binds each one to the correct ref.\n"
    "  • Block the characters with intentional spatial logic — who is "
    "foreground, who is background, who is the focal subject. Anime "
    "blocking favors clear separation over photoreal candid clustering.\n"
    "  • Each character keeps their canonical identity (face, hair, "
    "silhouette). NEVER substitute a generic descriptor for a "
    "`ref_image_N` label.\n"
    "  • Char limit bumps to 450 for multi-subject — each character "
    "needs a position + expression note."
)


# ── System prompts: video (i2v anime cadence) ────────────────────────────


_ANIME_VIDEO_SYSTEM = (
    "You are a motion prompt builder for an i2v anime pipeline (4-8s "
    "clip). The source still is the first frame — describe the "
    "animation that unfolds across the clip.\n\n"
    "ANIME CADENCE — match cadence to dramatic intent:\n"
    "  • Characters animate on twos or threes (cel-anime convention — "
    "preserves character readability without smooth-mo plasticity). "
    "Some sakuga action beats may run on ones for impact. Locked-off "
    "slow scenes lean on threes; dynamic action allows ones.\n"
    "  • Backgrounds animate on ones (foliage drift, water, dust "
    "motes, light flicker, fabric in wind) — these can be continuous.\n"
    "  • Avoid uniform 24fps character motion unless the moment is a "
    "sakuga action beat — that reads as 3DCG or rotoscope, not 2D "
    "anime.\n\n"
    "TIME-CODED BEATS for 4-8s clips:\n"
    "  • Choose a beat structure that fits the dramatic moment. "
    "Examples: 0-2s hold + 2-4s reaction + 4-6s gesture + 6-8s settle. "
    "Or sustained held expression with one micro-beat at the end. "
    "Match the script's emotional arc, not a checklist.\n"
    "  • Free choice — sequence beats only when the moment calls for "
    "it.\n\n"
    "ANTI-FREEZE (safety floor): SOMETHING visible must change between "
    "frame 0 and the clip end. Acceptable minimums: blink, breath "
    "rise, hair drift, fabric catch, eye-light shift. Adjective-only "
    "direction ('gentle softness') freezes the model — always pair "
    "feeling with a concrete observable change.\n\n"
    "CAMERA: anime camera vocabulary — slow push-in, slow pull-out, "
    "horizontal pan, vertical pan, rack focus, sakuga handheld for "
    "action beats, locked-off for dialogue. Pick the move that fits "
    "the beat; locked-off is a valid choice.\n\n"
    "ALWAYS INCLUDE (per character on screen): natural blinks at 2-4 "
    "second intervals, breath rise on the chest, hair edge drift. "
    "Background plates: subtle parallax or atmospheric motion (dust, "
    "steam, light shimmer) when present.\n\n"
    "AUDIO: silent by default. Anime production layers dialogue, ADR, "
    "foley, and music in post — i2v providers should NOT generate "
    "synthetic dialogue. If ambient SFX direction is appropriate, "
    "keep it diegetic and generic (room tone, wind, distant traffic). "
    "No lip-sync, no synth voice, no recognizable melody.\n\n"
    "BILINGUAL: upstream may be Vietnamese; output the motion prompt "
    "in English. Max 450 chars (600 multi-subject).\n\n"
    "Output the motion prompt only — no preamble."
)


_ANIME_VIDEO_MULTI_SUBJECT_CLAUSE = (
    "\n\nMULTI-SUBJECT MODE: Source frame has 2+ characters. Direct "
    "each character independently:\n"
    "  • Each character gets their own beat — no synchronized "
    "choreography unless the script explicitly calls for it. "
    "Asymmetric motion reads natural; mirrored motion reads staged.\n"
    "  • Anti-freeze applies PER character: every person in frame "
    "must show at least one observable change (blink, breath, "
    "micro-expression) over the clip.\n"
    "  • Reference each character by `ref_image_N` label (e.g. "
    "\"ref_image_1 turns slightly toward ref_image_2\"). Never "
    "substitute generic descriptors.\n"
    "  • Char limit bumps to 600 for multi-subject — each character "
    "needs their own direction."
)


# Static-camera variant — locks the camera, subject motion only.
_ANIME_VIDEO_STATIC_CLAUSE = (
    "\n\nCAMERA OVERRIDE: STATIC, locked-off. No push-in / pull-out / "
    "pan / zoom / rack focus / handheld. Subject + background plate "
    "motion only — the frame edges never move."
)


# ── System prompt: VN script → shot breakdown ────────────────────────────


_ANIME_SCRIPT_PARSE_SYSTEM = (
    "You are a storyboard supervisor for an anime production. The user "
    "will paste a Vietnamese (or any-language) scene script. Break it "
    "down into discrete cinematic shots.\n\n"
    "OUTPUT FORMAT — strict JSON object, no markdown fences, no "
    "preamble:\n"
    "{\n"
    "  \"shots\": [\n"
    "    {\n"
    "      \"order\": 1,\n"
    "      \"script_text\": \"<the original-language line(s) for this "
    "shot, verbatim from the user's input — do NOT translate>\",\n"
    "      \"camera_angle\": \"<one of: wide establishing | medium "
    "two-shot | medium | medium close-up | close-up | extreme "
    "close-up | low-angle | high-angle | over-the-shoulder | dutch "
    "tilt>\",\n"
    "      \"characters_in_frame\": [\"<character name as written in "
    "script>\"],\n"
    "      \"environment\": \"<short EN phrase summarizing the location "
    "/ time-of-day / atmosphere, e.g. 'rainy alley at night, neon "
    "reflections'>\",\n"
    "      \"dialogue\": \"<character_name>: <line>\" or null,\n"
    "      \"beat_notes\": \"<short EN note on dramatic intent — what "
    "the shot accomplishes narratively>\"\n"
    "    }\n"
    "  ]\n"
    "}\n\n"
    "RULES:\n"
    "  • Split aggressively — every camera change, every reaction "
    "shot, every cut is its own shot. A 4-line scene typically becomes "
    "6-12 shots, not 4.\n"
    "  • Preserve script_text verbatim in the source language. NO "
    "translation of dialogue or action lines.\n"
    "  • camera_angle, environment, beat_notes — ALWAYS in English "
    "(these feed downstream English-only prompt synthesis).\n"
    "  • characters_in_frame — use the character names exactly as "
    "written in the script (do not invent or romanize).\n"
    "  • dialogue — if the shot contains a spoken line, format as "
    "\"<character_name>: <line>\" with the line VERBATIM in the source "
    "language; otherwise null. Used by Phase 7+ lip-sync / subtitle "
    "work.\n"
    "  • Order shots in narrative sequence starting at 1.\n"
    "  • If the script is ambiguous about who is in frame for a beat, "
    "infer conservatively from context — prefer fewer characters over "
    "over-populating the frame.\n\n"
    "Output the JSON object only."
)


# Public re-exports of the prompt constants — tests import them.
ANIME_IMAGE_SYSTEM = _ANIME_IMAGE_SYSTEM
ANIME_VIDEO_SYSTEM = _ANIME_VIDEO_SYSTEM
ANIME_SCRIPT_PARSE_SYSTEM = _ANIME_SCRIPT_PARSE_SYSTEM


class PromptSynthError(RuntimeError):
    pass


# ── Ref-source node types ────────────────────────────────────────────────
# Mirror frontend ``REF_SOURCE_TYPES`` in ``store/generation.ts`` so the
# ``ref_image_N`` numbering we hand the LLM aligns with the actual
# positional slot Flow / Dreamina see on the wire. ``master_shot`` is
# Phase 6 new — it earns a dedicated ``establishing_shot_ref`` label
# instead of a numbered slot (it's always slot 1; character refs
# follow at 2+).
_REF_SOURCE_TYPES = {
    "character",
    "image",
    "visual_asset",
    "storyboard",
    "master_shot",
}


# ── Bible hierarchy lookup ───────────────────────────────────────────────


def _load_bibles_for_shot(session, shot_id: uuid.UUID) -> tuple[dict[str, Any], str]:
    """Walk Shot → Scene → Project and return (project_bible, scene_bible_text).

    Missing rows fall back to empty values — bare-bones projects without a
    bible filled in MUST NOT break ``auto_prompt``.
    """
    shot = session.get(Shot, shot_id) if shot_id else None
    if shot is None:
        return {}, ""
    scene = session.get(Scene, shot.scene_id)
    if scene is None:
        return {}, ""
    project = session.get(Project, scene.project_id)
    project_bible = dict(project.project_bible or {}) if project else {}
    return project_bible, scene.scene_bible_text or ""


def _format_bible_block(
    project_bible: dict[str, Any], scene_bible_text: str
) -> str:
    """Render the Project + Scene Bible as a prepend block.

    Empty fields are dropped silently so a half-filled bible still
    surfaces what it has without injecting noisy "(none)" lines.
    Returns "" when both bibles are entirely empty — caller can then
    skip prepending altogether.
    """
    proj_lines: list[str] = []
    art_style = project_bible.get("art_style")
    if isinstance(art_style, str) and art_style.strip():
        proj_lines.append(f"  Art style: {art_style.strip()}")
    palette = project_bible.get("color_palette")
    if isinstance(palette, list) and palette:
        proj_lines.append(
            "  Palette: "
            + ", ".join(str(p) for p in palette if isinstance(p, str) and p)
        )
    line_style = project_bible.get("line_style")
    if isinstance(line_style, str) and line_style.strip():
        proj_lines.append(f"  Line style: {line_style.strip()}")
    lighting = project_bible.get("lighting_conventions")
    if isinstance(lighting, str) and lighting.strip():
        proj_lines.append(f"  Lighting: {lighting.strip()}")
    negative = project_bible.get("negative_prompts")
    if isinstance(negative, list) and negative:
        proj_lines.append(
            "  Negative: "
            + ", ".join(str(p) for p in negative if isinstance(p, str) and p)
        )

    parts: list[str] = []
    if proj_lines:
        parts.append(
            "PROJECT BIBLE (style anchor — apply to every visual "
            "decision):\n" + "\n".join(proj_lines)
        )
    if scene_bible_text and scene_bible_text.strip():
        parts.append(
            "SCENE BIBLE (spatial / atmospheric anchor for this "
            "scene):\n  " + scene_bible_text.strip()
        )
    return "\n\n".join(parts)


# ── Upstream graph walk ──────────────────────────────────────────────────


def _resolve_master_shot_media(session, data: dict) -> Optional[str]:
    """Resolve a MasterShotNode's media_id.

    Frontend stores ``masterShotAssetId`` (numeric Asset PK) and
    sometimes ``mediaId`` (Asset.uuid_media_id) if previously
    cached. Prefer ``mediaId`` when present; otherwise look up the
    asset.
    """
    direct = data.get("mediaId")
    if isinstance(direct, str) and direct:
        return direct
    asset_id = data.get("masterShotAssetId")
    if isinstance(asset_id, int) and asset_id > 0:
        asset = session.get(Asset, asset_id)
        if asset is not None and asset.uuid_media_id:
            return asset.uuid_media_id
    return None


def _collect_upstream(
    node_id: int,
) -> tuple[list[dict], Optional[Node], dict[str, Any], str]:
    """Return ``(upstream_records, target_node, project_bible, scene_bible_text)``.

    Each record carries:
      - ``type``         — node type
      - ``shortId``      — internal node identifier (multi-subject
        detection only; never surfaced to the LLM in the prompt body)
      - ``ref_label``    — ``"establishing_shot_ref"``, ``"ref_image_N"``
        (1-based), or ``None`` for non-ref records (prompt / note /
        script / bible_ref nodes).
      - ``brief``        — the description text (prompt > aiBrief > title)
      - ``prompt``       — user-typed prompt (also surfaces under the
        Direction section for ``prompt`` nodes)
      - ``title``
      - ``has_media``    — whether the node carries a binding mediaId/mediaIds
      - ``subject_chars``— shortIds of character grandparents (for
        multi-subject detection through image siblings)
      - ``script_text``  — set for ``script`` nodes (passthrough text)
      - ``bible_text``   — set for ``bible_ref`` nodes (passthrough text)

    Slot ordering for ref records: a single ``master_shot`` upstream
    takes ``establishing_shot_ref`` (priority slot). Other ref-source
    types get ``ref_image_1, ref_image_2, ...`` in edge-insertion order.
    """
    with get_session() as s:
        target = s.get(Node, node_id)
        if target is None:
            return [], None, {}, ""

        project_bible, scene_bible_text = _load_bibles_for_shot(s, target.shot_id)

        edges = s.exec(
            select(Edge).where(Edge.target_id == node_id).order_by(Edge.id)
        ).all()
        upstream_ids = [e.source_id for e in edges]

        records: list[dict] = []
        next_ref_index = 1
        for uid in upstream_ids:
            n = s.get(Node, uid)
            if n is None:
                continue
            data = n.data or {}
            ai_brief = data.get("aiBrief") if isinstance(data.get("aiBrief"), str) else None
            user_prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else None
            brief = user_prompt or ai_brief

            # Passthrough text on the new anime node types.
            script_text = data.get("scriptText") if isinstance(data.get("scriptText"), str) else None
            bible_text = data.get("bibleText") if isinstance(data.get("bibleText"), str) else None

            # Multi-subject detection through image siblings (couple shot
            # with char_m → img_m, char_f → img_f, both feeding target).
            subject_chars: list[str] = []
            if n.type == "image":
                gp_edges = s.exec(
                    select(Edge).where(Edge.target_id == uid).order_by(Edge.id)
                ).all()
                for ge in gp_edges:
                    gp = s.get(Node, ge.source_id)
                    if gp is not None and gp.type == "character":
                        subject_chars.append(gp.short_id)

            # Resolve has_media: accept singular `mediaId` or variant list.
            # MasterShot also accepts indirect resolution from masterShotAssetId.
            mids = data.get("mediaIds")
            base_has_media = bool(
                (isinstance(data.get("mediaId"), str) and data.get("mediaId"))
                or (isinstance(mids, list) and any(isinstance(m, str) and m for m in mids))
            )
            if not base_has_media and n.type == "master_shot":
                base_has_media = _resolve_master_shot_media(s, data) is not None

            ref_label: Optional[str] = None
            if n.type == "master_shot" and base_has_media:
                ref_label = "establishing_shot_ref"
            elif n.type in _REF_SOURCE_TYPES and base_has_media:
                ref_label = f"ref_image_{next_ref_index}"
                next_ref_index += 1

            records.append(
                {
                    "type": n.type,
                    "shortId": n.short_id,
                    "ref_label": ref_label,
                    "brief": brief if isinstance(brief, str) else None,
                    "prompt": user_prompt,
                    "title": data.get("title") if isinstance(data.get("title"), str) else None,
                    "has_media": base_has_media,
                    "subject_chars": subject_chars,
                    "script_text": script_text,
                    "bible_text": bible_text,
                }
            )
        return records, target, project_bible, scene_bible_text


def _distinct_subjects(records: list[dict]) -> list[str]:
    """Ordered list of distinct character shortIds across upstream.

    Counts ``character`` nodes by their own shortId, and ``image`` nodes
    by the shortIds of their character grandparents.
    """
    seen_set: set[str] = set()
    ordered: list[str] = []
    for r in records:
        ids: list[str] = []
        if r["type"] == "character":
            ids = [r["shortId"]]
        elif r["type"] == "image":
            ids = list(r.get("subject_chars") or [])
        for sid in ids:
            if sid and sid not in seen_set:
                seen_set.add(sid)
                ordered.append(sid)
    return ordered


def _image_system_prompt(subject_count: int) -> str:
    if subject_count >= 2:
        return _ANIME_IMAGE_SYSTEM + _ANIME_IMAGE_MULTI_SUBJECT_CLAUSE
    return _ANIME_IMAGE_SYSTEM


def _video_system_prompt(camera: Optional[str], subject_count: int = 1) -> str:
    base = _ANIME_VIDEO_SYSTEM
    if subject_count >= 2:
        base = base + _ANIME_VIDEO_MULTI_SUBJECT_CLAUSE
    if camera == "static":
        base = base + _ANIME_VIDEO_STATIC_CLAUSE
    return base


# ── User-message assembly ────────────────────────────────────────────────


def _format_user_message(
    records: list[dict],
    target: Node,
    project_bible: dict[str, Any],
    scene_bible_text: str,
) -> str:
    """Render the upstream context + bibles into a compact prompt.

    Bible block prepends (silently empty when bible isn't filled in).
    Reference images are labeled positionally: ``establishing_shot_ref``
    for the scene's master shot, ``ref_image_N`` for other ref sources.
    ``script`` and ``bible_ref`` passthrough nodes surface their text
    under dedicated sections so the LLM has the dramatic / style
    context driving this shot.
    """
    parts: list[str] = []

    bible_block = _format_bible_block(project_bible, scene_bible_text)
    if bible_block:
        parts.append(bible_block)

    # Build the label-translation map for image-sibling multi-subject
    # detection (so an `image` record's `subject_chars` entries can be
    # rendered as "same subject as ref_image_2" instead of #shortId).
    label_for_short_id: dict[str, str] = {}
    for r in records:
        if r.get("ref_label"):
            label_for_short_id[r["shortId"]] = r["ref_label"]

    by_section: dict[str, list[str]] = {}
    for r in records:
        # Passthrough text on the new anime node types lives under
        # dedicated sections — never gets a ref label.
        if r["type"] == "script":
            txt = r.get("script_text") or r.get("brief") or r.get("title")
            if txt:
                by_section.setdefault("script", []).append(f"- {txt}")
            continue
        if r["type"] == "bible_ref":
            txt = r.get("bible_text") or r.get("brief") or r.get("title")
            if txt:
                by_section.setdefault("bible_ref", []).append(f"- {txt}")
            continue
        if r["type"] == "note":
            # Notes stay decorative — never surface.
            continue

        text = r["brief"] or r["prompt"] or r["title"] or "(no description)"

        suffix = ""
        if r["type"] == "image" and r.get("subject_chars"):
            translated = [
                label_for_short_id[c]
                for c in r["subject_chars"]
                if c in label_for_short_id
            ]
            if translated:
                suffix = f"  [same subject as {', '.join(translated)}]"

        label = r.get("ref_label")
        if label:
            line = f"{label}: {text}{suffix}"
        else:
            line = f"- {text}"
        by_section.setdefault(r["type"], []).append(line)

    if by_section.get("master_shot"):
        parts.append(
            "Establishing shot (scene master — match composition / "
            "lighting / palette):\n  - " + "\n  - ".join(by_section["master_shot"])
        )
    if by_section.get("character"):
        parts.append(
            "Subject(s) (character):\n  - "
            + "\n  - ".join(by_section["character"])
        )
    if by_section.get("visual_asset"):
        parts.append(
            "Prop / wardrobe / object (visual_asset):\n  - "
            + "\n  - ".join(by_section["visual_asset"])
        )
    if by_section.get("image"):
        parts.append(
            "Reference image(s):\n  - "
            + "\n  - ".join(by_section["image"])
        )
        # When 2+ image refs feed in, the LLM needs to classify each by
        # role (subject / wardrobe / setting / environment) so a location
        # ref doesn't get silently dropped as "just another reference".
        if len(by_section["image"]) >= 2:
            parts.append(
                "ROLE INFERENCE: For each reference image above, infer "
                "its role from the brief. Briefs describing characters "
                "or props → subject / wardrobe reference. Briefs "
                "describing places / environments → SETTING reference "
                "(use as the shot's background). Compose a single anime "
                "frame that places the characters INTO any setting "
                "reference present — never silently drop a location ref."
            )
    if by_section.get("script"):
        parts.append(
            "Script (this shot — drives camera angle + character "
            "action; may be Vietnamese):\n  " + "\n  ".join(by_section["script"])
        )
    if by_section.get("bible_ref"):
        parts.append(
            "Bible reference (additional style / spatial context from a "
            "BibleRef node):\n  " + "\n  ".join(by_section["bible_ref"])
        )
    if by_section.get("prompt"):
        parts.append(
            "Direction / style notes (prompt nodes — apply as styling "
            "guidance):\n  - " + "\n  - ".join(by_section["prompt"])
        )

    # Multi-subject detector.
    subjects = _distinct_subjects(records)
    if len(subjects) >= 2:
        parts.append(
            f"DISTINCT SUBJECTS DETECTED: {len(subjects)} characters. "
            "Treat as a single multi-subject anime frame; describe each "
            "character's placement using the `ref_image_N` labels above."
        )

    target_data = target.data or {}
    target_title = target_data.get("title") if isinstance(target_data.get("title"), str) else None
    if target_title:
        parts.append(f"Target node title (hint): {target_title}")

    if not parts:
        return (
            f"Target: {target_title or 'anime frame'}\n\n"
            "Write a generic cinematic 2D anime cel-shaded frame prompt."
        )
    return "\n\n".join(parts) + "\n\nReturn only the prompt sentence."


# ── Batch (variant) addendum ─────────────────────────────────────────────


_BATCH_SUFFIX = (
    "\n\nBATCH MODE: Output a JSON ARRAY of EXACTLY {count} distinct "
    "prompts. Each prompt MUST vary on a different visual axis (camera "
    "angle, character expression, blocking, lighting accent) — no two "
    "variants may resolve to the same composition. Output ONLY the JSON "
    "array, no preamble, no markdown fences. Each prompt still respects "
    "the anime style floor + char cap. Example:\n"
    "[\n"
    "  \"Wide establishing anime cel shot, …\",\n"
    "  \"Medium close-up, low-angle hero, …\",\n"
    "  …\n"
    "]"
)


# ── Storyboard addendum (anime narrative beats) ──────────────────────────


_STORYBOARD_SUFFIX = (
    "\n\nSTORYBOARD MODE: Output ONE JSON OBJECT with exactly these "
    "keys:\n"
    "  \"prompts\": array of EXACTLY {count} strings (≤380 chars each),\n"
    "                each describing one beat of a continuous narrative —\n"
    "                index 0 is the first beat, index {count}-1 the last.\n"
    "  \"parents\": array of EXACTLY {count} entries, each null OR an integer.\n"
    "                parents[k] = null  → beat k is a NEW SCENE/ROOT (will be\n"
    "                  generated fresh — use ONLY when location/character/\n"
    "                  visual context legitimately changes from the prior beat).\n"
    "                parents[k] = j (0 ≤ j < k) → beat k VISUALLY CONTINUES\n"
    "                  from beat j — same location, same characters on-model,\n"
    "                  same lighting carry-over. The image will be EDITED\n"
    "                  from beat j's output, so beat k's prompt MUST describe\n"
    "                  ONLY THE DELTA (e.g. \"now turns toward the door\",\n"
    "                  \"now the rain starts\") — DO NOT re-describe identity,\n"
    "                  location, or lighting.\n"
    "                Constraints: parents[0] MUST be null; parents[k] < k.\n"
    "Coherence rules (every beat):\n"
    "  • SAME character identity across the sequence — anchor on\n"
    "    `ref_image_1` (or `establishing_shot_ref` if present) when a\n"
    "    person reference exists.\n"
    "  • SAME location + lighting within a continuity chain.\n"
    "  • Each beat is a discrete cinematic shot — character reaction,\n"
    "    line of dialogue beat, cut to next angle, environmental detail.\n"
    "Per-beat:\n"
    "  • 2D anime cel-shaded frame, explicit camera angle, on-model\n"
    "    character identity.\n"
    "  • each beat advances the story; no two beats interchangeable.\n"
    "{narrative_seed_block}"
    "Output ONLY the JSON object — no preamble, no markdown fences. Example:\n"
    "{\n"
    "  \"prompts\": [\n"
    "    \"Wide establishing cel anime shot, rainy alley at night, …\",\n"
    "    \"Cut to medium close-up of ref_image_1, eyes narrowing, …\",\n"
    "    \"Over-the-shoulder reverse, ref_image_2 steps into frame, …\",\n"
    "    \"Close-up, ref_image_1 reaches for the door handle, …\"\n"
    "  ],\n"
    "  \"parents\": [null, 0, 1, 2]\n"
    "}"
)


# ── Public API ───────────────────────────────────────────────────────────


async def auto_prompt_storyboard(
    node_id: int,
    count: int,
    *,
    narrative_seed: str = "",
) -> dict:
    """Compose N anime narrative beats with a continuity tree in one LLM call.

    Returns ``{"prompts": [str * count], "parents": [int|None * count]}``.
    """
    if not 1 <= count <= 8:
        raise PromptSynthError(f"storyboard count must be 1..8, got {count}")

    records, target, project_bible, scene_bible_text = _collect_upstream(node_id)
    if target is None:
        raise PromptSynthError(f"node {node_id} not found")

    subject_count = len(_distinct_subjects(records))
    base_system = _image_system_prompt(subject_count)
    seed = (narrative_seed or "").strip()
    seed_block = (
        f"\nNarrative seed (user intent — beats MUST follow this arc):\n"
        f"  {seed}\n\n"
        if seed
        else ""
    )
    suffix = (
        _STORYBOARD_SUFFIX
        .replace("{count}", str(count))
        .replace("{narrative_seed_block}", seed_block)
    )
    system_prompt = base_system + suffix
    user_msg = _format_user_message(records, target, project_bible, scene_bible_text)

    async with record_activity(
        "auto_prompt_storyboard",
        params={
            "node_id": node_id,
            "count": count,
            "narrative_seed": seed[:200],
        },
        node_id=node_id,
    ) as activity:
        try:
            text = await run_llm(
                "auto_prompt", user_msg, system_prompt=system_prompt, timeout=120.0
            )
        except LLMError as exc:
            raise PromptSynthError(
                f"auto-prompt provider failed: {exc}"
            ) from exc

        text = _strip_fences(text)

        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PromptSynthError(
                f"storyboard provider returned non-JSON: {text[:200]!r}"
            ) from exc
        if not isinstance(obj, dict):
            raise PromptSynthError("storyboard response is not a JSON object")

        raw_prompts = obj.get("prompts")
        raw_parents = obj.get("parents")
        if not isinstance(raw_prompts, list) or not isinstance(raw_parents, list):
            raise PromptSynthError(
                "storyboard response missing prompts[] / parents[]"
            )
        if len(raw_prompts) != count or len(raw_parents) != count:
            raise PromptSynthError(
                f"storyboard length mismatch: prompts={len(raw_prompts)} "
                f"parents={len(raw_parents)} expected={count}"
            )

        prompts: list[str] = []
        for i, p in enumerate(raw_prompts):
            if not isinstance(p, str) or not p.strip():
                raise PromptSynthError(f"storyboard prompts[{i}] empty/non-str")
            prompts.append(p.strip())

        parents: list[Optional[int]] = []
        for k, v in enumerate(raw_parents):
            if v is None:
                parents.append(None)
                continue
            if isinstance(v, bool) or not isinstance(v, int):
                raise PromptSynthError(
                    f"storyboard parents[{k}]={v!r} must be int or null"
                )
            if not 0 <= v < k:
                raise PromptSynthError(
                    f"storyboard parents[{k}]={v} out of range [0, {k})"
                )
            parents.append(v)

        if parents[0] is not None:
            raise PromptSynthError("storyboard parents[0] must be null (root)")

        result = {"prompts": prompts, "parents": parents}
        activity.set_result(result)
        return result


async def auto_prompt_batch(
    node_id: int, count: int, *, camera: Optional[str] = None
) -> list[str]:
    """Compose N variant-distinct anime prompts in a single LLM call."""
    if count < 1:
        raise PromptSynthError("count must be >= 1")
    if count == 1:
        single = await auto_prompt(node_id, camera=camera)
        return [single]

    records, target, project_bible, scene_bible_text = _collect_upstream(node_id)
    if target is None:
        raise PromptSynthError(f"node {node_id} not found")

    is_video = target.type == "video"
    subject_count = len(_distinct_subjects(records))
    if is_video:
        base_system = _video_system_prompt(camera, subject_count)
    else:
        base_system = _image_system_prompt(subject_count)
    system_prompt = base_system + _BATCH_SUFFIX.format(count=count)
    user_msg = _format_user_message(records, target, project_bible, scene_bible_text)

    async with record_activity(
        "auto_prompt_batch",
        params={"node_id": node_id, "count": count, "camera": camera},
        node_id=node_id,
    ) as activity:
        try:
            text = await run_llm(
                "auto_prompt", user_msg, system_prompt=system_prompt, timeout=120.0
            )
        except LLMError as exc:
            raise PromptSynthError(f"auto-prompt provider failed: {exc}") from exc

        text = _strip_fences(text)

        try:
            arr = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PromptSynthError(
                f"auto-prompt provider returned non-JSON for batch: {text[:200]!r}"
            ) from exc
        if not isinstance(arr, list):
            raise PromptSynthError("auto-prompt batch response is not a JSON array")
        prompts = [str(p).strip() for p in arr if isinstance(p, str) and p.strip()]
        if not prompts:
            raise PromptSynthError("auto-prompt batch returned no valid prompts")
        while len(prompts) < count:
            prompts.append(prompts[-1])
        prompts = prompts[:count]
        activity.set_result({"prompts": prompts})
        return prompts


async def auto_prompt(node_id: int, *, camera: Optional[str] = None) -> str:
    """Compose a single generation prompt by walking upstream + bibles.

    Branches by target type:
    - ``image`` (default) → anime composition prompt
    - ``video`` → motion prompt (with optional ``camera="static"`` lock)
    """
    records, target, project_bible, scene_bible_text = _collect_upstream(node_id)
    if target is None:
        raise PromptSynthError(f"node {node_id} not found")

    is_video = target.type == "video"
    subject_count = len(_distinct_subjects(records))
    if is_video:
        system_prompt = _video_system_prompt(camera, subject_count)
    else:
        system_prompt = _image_system_prompt(subject_count)
    user_msg = _format_user_message(records, target, project_bible, scene_bible_text)

    async with record_activity(
        "auto_prompt",
        params={"node_id": node_id, "camera": camera},
        node_id=node_id,
    ) as activity:
        try:
            text = await run_llm(
                "auto_prompt",
                user_msg,
                system_prompt=system_prompt,
                timeout=90.0,
            )
        except LLMError as exc:
            raise PromptSynthError(f"auto-prompt provider failed: {exc}") from exc

        text = (text or "").strip().strip('"').strip("'")
        if not text:
            raise PromptSynthError("empty response from auto-prompt provider")
        if len(text) > 600:
            text = text[:600].rstrip() + "…"
        activity.set_result({"prompt": text})
        return text


# ── Script → shot breakdown (Phase 6.4) ──────────────────────────────────


async def parse_script(scene_id: uuid.UUID, script_text: str) -> list[dict]:
    """Parse a VN (or any-language) scene script into structured shot
    breakdowns via the configured Auto-Prompt provider.

    Returns a list of shot dicts with keys ``order``, ``script_text``,
    ``camera_angle``, ``characters_in_frame``, ``environment``,
    ``dialogue``, ``beat_notes``. Used by ``/api/prompt/parse-script``
    and surfaced in the frontend ScriptInputDialog.

    The ``scene_id`` is currently used only as a logging breadcrumb +
    activity-feed correlation. Bible injection on parse is deferred —
    the LLM has enough context from the script alone to produce a clean
    breakdown; project/scene style notes apply when the resulting shots
    are later rendered.
    """
    text = (script_text or "").strip()
    if not text:
        raise PromptSynthError("script_text is empty")

    async with record_activity(
        "parse_script",
        params={"scene_id": str(scene_id), "len": len(text)},
    ) as activity:
        try:
            raw = await run_llm(
                "auto_prompt",
                text,
                system_prompt=_ANIME_SCRIPT_PARSE_SYSTEM,
                timeout=120.0,
            )
        except LLMError as exc:
            raise PromptSynthError(f"parse-script provider failed: {exc}") from exc

        raw = _strip_fences(raw)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PromptSynthError(
                f"parse-script returned non-JSON: {raw[:200]!r}"
            ) from exc
        if not isinstance(obj, dict):
            raise PromptSynthError("parse-script response is not a JSON object")
        shots = obj.get("shots")
        if not isinstance(shots, list):
            raise PromptSynthError("parse-script response missing shots[]")

        cleaned: list[dict] = []
        for i, sh in enumerate(shots):
            if not isinstance(sh, dict):
                raise PromptSynthError(f"shots[{i}] is not an object")
            script_text_field = sh.get("script_text")
            if not isinstance(script_text_field, str) or not script_text_field.strip():
                raise PromptSynthError(f"shots[{i}].script_text empty / non-str")
            order_raw = sh.get("order")
            order_val = order_raw if isinstance(order_raw, int) and order_raw > 0 else i + 1
            chars = sh.get("characters_in_frame") or []
            if not isinstance(chars, list):
                chars = []
            cleaned.append(
                {
                    "order": order_val,
                    "script_text": script_text_field.strip(),
                    "camera_angle": str(sh.get("camera_angle") or "").strip(),
                    "characters_in_frame": [
                        str(c).strip()
                        for c in chars
                        if isinstance(c, str) and c.strip()
                    ],
                    "environment": str(sh.get("environment") or "").strip(),
                    "dialogue": (
                        sh.get("dialogue")
                        if isinstance(sh.get("dialogue"), str) and sh.get("dialogue").strip()
                        else None
                    ),
                    "beat_notes": str(sh.get("beat_notes") or "").strip(),
                }
            )
        cleaned.sort(key=lambda s: s["order"])
        activity.set_result({"count": len(cleaned)})
        return cleaned


# ── helpers ──────────────────────────────────────────────────────────────


def _strip_fences(text: Optional[str]) -> str:
    """Strip Markdown code fences a provider may have added despite the
    'no preamble, no fences' instruction. Used by all JSON-output paths.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.lstrip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    return text
