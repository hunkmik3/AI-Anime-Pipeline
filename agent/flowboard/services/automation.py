"""Drama-film automation, behind ``/automation``.

A raw premise goes in; a breakdown comes out — cast, places, sequences. Each
sequence is then cut into shots, the cast and places get reference plates, and
each sequence becomes one clip.

Three engines, each doing the one thing it can:

* The breakdown and the shot cuts are TEXT, so they run through the
  feature-routed LLM (``planner``). Atrium is an image gateway with no text
  endpoint; it cannot do this half.
* The plates are IMAGES, through Atrium's Gemini models.
* The clips are VIDEO, through Seedance 2.5 on the existing Avis provider.

The split that matters everywhere: the model returns *content* (who someone
is, what they wear, what happens at 0-3s), and this module assembles it into a
fixed prompt template. A template fix is one edit here, not a re-run of every
generation.

Generated media is published to R2 and the board stores the URL. Pixels are
too big to keep in a saved board, and a data URL dies with the tab that made
it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import tempfile
import unicodedata
import uuid
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Optional

from flowboard.services.flowstudio import atrium_api, avis_api, r2
from flowboard.services.llm.registry import run_llm

logger = logging.getLogger(__name__)


class AutomationError(RuntimeError):
    """Breakdown or plate generation failed in a way the caller should show."""


# The style tail is the production's signature — every plate and every sheet
# ends with it, so the whole batch reads as one film. Two variants because a
# turnaround sheet is a studio document (no aspect clause, white sweep) while a
# plate is a frame from the film (aspect clause, no characters).
STYLE_TAIL_SHEET = (
    "Cinematic Realistic, Hollywood cinematic drama film style, ultra-detailed, "
    "8K resolution, no watermark, no extra props, no background environment, "
    "minimal or no text labels."
)
STYLE_TAIL_PLATE = (
    "Cinematic Realistic, Hollywood cinematic drama film, ultra-detailed, "
    "8K resolution, {aspect} composition, no main characters, no readable text, "
    "no logos, no subtitles, no watermark."
)

# A sheet is a DOCUMENT with a fixed layout, not "several views of a person".
# Asking loosely returned loosely: four bodies at four scales, a headshot column
# that was really four expressions, and costume asymmetry that swapped sides
# between panels — each of which makes the sheet useless as a reference.
# So the layout is specified the way a call sheet is: the split, the panel
# count, the angle of each panel, and what each panel must NOT be.
TURNAROUND_BLOCK = (
    "LAYOUT — a landscape production sheet on a clean, uncluttered background, "
    "split 60 / 40.\n"
    "LEFT 60% — THREE full-body views in a row, equally scaled: 1. straight front, "
    "2. strict 90-degree side profile, 3. straight back. All three are the SAME "
    "character in the SAME pose seen from different directions — rotate the "
    "viewpoint around one fixed pose, do not invent a new pose per view. Align the "
    "figures at the same head height and the same foot line. Near-orthographic, no "
    "wide-angle distortion. Show the complete figure including all hair, fabric, "
    "hands and footwear — nothing cropped, no overlapping figures, no extra "
    "full-body views.\n"
    "RIGHT 40% — FOUR head-and-shoulder studies in a clean 2x2 grid, all at the same "
    "head scale and the same crop. TOP LEFT front, facing the viewer, both eyes and "
    "the hairline visible. TOP RIGHT strict rear, facing directly away: the back of "
    "the hairstyle and the nape, with NO face visible, no glance over the shoulder, "
    "no three-quarter turn. BOTTOM LEFT a clear 45-degree three-quarter. BOTTOM RIGHT "
    "a strict 90-degree profile showing the forehead-to-nose silhouette, lips, chin "
    "and jawline.\n"
    "These are FOUR VIEWING ANGLES, not four expressions: hold one calm, consistent "
    "expression wherever the face is visible.\n"
    "CONTINUITY — identical facial identity, age, proportions, hairline, hairstyle, "
    "costume construction, accessories and footwear in every panel. Every asymmetric "
    "feature stays on the SAME anatomical side in all views; never mirror or relocate "
    "a costume detail between panels.\n"
    "Exactly THREE full-body views and FOUR head studies. No extra panels, no "
    "expression grid, no diagrams, no typography, no labels, no decorative borders."
)
# Named per material, because a model told only "realistic textures" gives every
# surface the same plastic sheen — skin, silk and steel alike.
MATERIAL_BLOCK = (
    "MATERIALS — read each one differently. Skin: smooth and luminous with subtle "
    "subsurface softness, never waxy, oily or plastic. Cloth: directional sheen and "
    "real folds, with visible seams, hems and thickness. Fitted dark fabric: "
    "restrained satin response, not glossy latex. Metal: crisp controlled "
    "reflections and readable engraved edges. Translucent fabric: layered "
    "transparency with visible hems. Hair: fine strand structure with silky "
    "directional highlights, never a sculpted plastic shell."
)
FORMAT_BLOCK = (
    "16:9 landscape character sheet.\n"
    "Neutral medium-grey studio background.\n"
    "Neutral studio lighting."
)


def _is_carried(piece: dict) -> bool:
    """Held or slung, not worn — see the CARRIED section in the sheet prompt."""
    text = " ".join(str(piece.get(k) or "") for k in ("piece", "detail")).lower()
    return any(w in text for w in
               ("backpack", "rucksack", "satchel", "handbag", "purse", "tote",
                "briefcase", "duffel", "case", "bag"))


def _is_footwear(piece: dict) -> bool:
    """Shoes get their own heading: it is the part a sheet most often fudges."""
    text = " ".join(str(piece.get(k) or "") for k in ("piece", "detail")).lower()
    return any(w in text for w in
               ("shoe", "boot", "loafer", "sneaker", "sandal", "heel", "footwear", "oxford"))


def _costume_line(piece: dict) -> str:
    """One garment, one line — material and colour first, then what makes it itself."""
    head = " · ".join(str(piece.get(k)).strip() for k in ("piece", "material", "colour")
                      if str(piece.get(k) or "").strip())
    detail = str(piece.get("detail") or "").strip()
    return f"{head} — {detail}" if detail else head


# A turnaround is only usable if the pose is the SAME pose seen three times. Left
# unsaid, the model gives each view its own pose and the sheet stops being a
# reference and becomes three drawings of a person.
POSE_BLOCK = (
    "The character stands on a flat floor in a composed, upright stance, weight "
    "even, shoulders level, chin level. Both arms hang slightly away from the "
    "body with elbows barely bent and hands relaxed, fingers naturally separated. "
    "Legs straight, one foot half a step ahead of the other, both feet flat on the "
    "ground and fully visible. Keep the pose restrained and readable — a "
    "turnaround, not a photograph. The same limb positions appear in the front, "
    "side and back views. No walking, leaning, hand on hip, crossed arms, seated, "
    "kneeling or action pose."
)
# A location has its own material list: a set drawing that treats stone, glass
# and polished floor identically reads as a render test, not as a place.
PLATE_MATERIAL_BLOCK = (
    "Read every surface differently. Stone and plaster: matte, with real grain and "
    "wear at the edges. Polished floor: sharp reflections that fall off with "
    "distance. Glass: transmission plus a thin surface reflection, never an opaque "
    "panel. Metal: controlled specular with readable edges. Fabric: soft "
    "directional sheen and weight. Foliage and water: translucency and movement, "
    "not flat colour."
)
SHEET_EXCLUSIONS = (
    "EXCLUDE — cropped limbs, a different pose per view, mirrored or relocated "
    "costume asymmetry, a face in the rear head study, an expression grid in place "
    "of the four head angles, inconsistent identity between panels, extra limbs, "
    "malformed hands, blurred detail, watermarks, captions or text of any kind."
)
SHEET_TECHNICAL_BLOCK = (
    "Use a solid white background, soft even studio lighting, centered layout, full "
    "body visible from head to toe in each main view, photorealistic skin texture, "
    "realistic fabric detail, clean presentation, and sharp professional "
    "character-sheet organization. The overall result should feel like a realistic "
    "production design reference sheet for a live-action film character."
)
PLATE_CAMERA_BLOCK = (
    "Eye-level camera, 24-28mm wide-angle lens, balanced composition, realistic "
    "spatial depth, soft cinematic lighting, photorealistic textures, subtle film "
    "grain, high dynamic range."
)

# A board is either live-action (the house style above) or a re-make of a
# reference video in anime. Everything that says "realistic" in a prompt reads
# from here, so an anime board never asks for photoreal skin on a model sheet.
STYLES: dict[str, dict[str, str]] = {
    "realistic": {
        "sheet_noun": "realistic character sheet",
        "sheet_technical": SHEET_TECHNICAL_BLOCK,
        "sheet_tail": STYLE_TAIL_SHEET,
        "wardrobe_rule": "keep the clothing simple, realistic and functional",
        "wardrobe_default": "Dress them in plain, realistic, functional clothing appropriate to the role.",
        "plate_opening": "Cinematic realistic wide establishing shot of",
        "plate_materials": "Realistic materials and surfaces, natural shadows, muted grounded tones.",
        "plate_camera": PLATE_CAMERA_BLOCK,
        "plate_tail": STYLE_TAIL_PLATE,
        "frame_tail": (
            "Cinematic Realistic, Hollywood cinematic drama film still, ultra-detailed, "
            "{aspect} composition, natural film grain, no readable text, no logos, "
            "no subtitles, no watermark, no border."
        ),
        "video_style": "Cinematic realistic, Hollywood cinematic drama film",
        "video_texture": "shallow depth of field, motivated practical lighting, realistic physical textures and natural skin detail",
    },
    # Written after a first pass came back as painted concept art. An image
    # model reads "3D CGI animated feature" as a genre, not as a renderer; what
    # actually moves it onto rendered geometry is naming the pipeline (engine,
    # shaders, passes) and forbidding the painting it would otherwise make.
    "cg3d": {
        "sheet_noun": "3D CGI character model sheet — a rendered turntable of one finished 3D character asset",
        "sheet_technical": (
            "Render it as a real 3D asset, not a drawing: physically based shading with "
            "measurable roughness and specular response, subsurface-scattering skin with pore "
            "detail, groomed hair cards with flyaways, simulated cloth with real weight, seams "
            "and thickness, ray-traced soft shadows and ambient occlusion, global illumination "
            "from a neutral three-point studio setup, subtle depth of field. Solid neutral grey "
            "studio backdrop, centred layout, full body head to toe in each main view, identical "
            "proportions and materials across every view. "
            # Naming the tools is what moved the output off painted concept art and onto
            # rendered geometry — measured by generating the same sheet with and without
            # this sentence on both engines.
            "This is a finished 3D asset photographed in an engine viewport: Unreal Engine 5 "
            "real-time render, MetaHuman-grade skin shader with pore and peach-fuzz detail, "
            "Marvelous Designer cloth simulation with stitched seams and real thickness, XGen "
            "hair groom with individual strands, anisotropic highlights on metal, screen-space "
            "reflections, ray-traced ambient occlusion, studio HDRI lighting. It must look "
            "RENDERED, not painted."
        ),
        "sheet_tail": (
            "Modern 3D CGI animated-feature render, stylised-realistic proportions, ray-traced "
            "global illumination, no watermark, no extra props, no background environment, "
            "minimal or no text labels. NOT a painting, NOT concept art, NOT a 2D illustration, "
            "no visible brush strokes, no line art, no cel shading, no anime styling."
        ),
        "wardrobe_rule": (
            "build the costume as simulated geometry — real fabric weight, folds that fall with "
            "gravity, stitched seams, metal with weathered edges — exactly as described"
        ),
        "wardrobe_default": (
            "Dress them in a costume that fits the role, built as simulated cloth and modelled "
            "hard-surface pieces with readable material layers."
        ),
        "plate_opening": "A rendered 3D CGI environment plate — a still frame from a modern animated feature — of",
        "plate_materials": (
            "Physically based materials with real roughness and displacement, ray-traced global "
            "illumination and bounce light, contact-hardening shadows, ambient occlusion in the "
            "crevices, volumetric light and atmospheric scattering."
        ),
        "plate_camera": (
            "Eye-level virtual camera, 24-28mm lens, balanced composition, believable spatial "
            "depth with distinct foreground, midground and background planes, cinematic depth of "
            "field with clean bokeh."
        ),
        "plate_tail": (
            "Modern 3D CGI animated-feature render, Unreal Engine 5 / Octane quality, 8K, "
            "{aspect} composition, no main characters, no readable text, no logos, no subtitles, "
            "no watermark. NOT a matte painting, NOT concept art, NOT a 2D illustration, no "
            "visible brush strokes."
        ),
        "frame_tail": (
            "Modern 3D CGI animated-feature frame, ray-traced global illumination, physically "
            "based materials, {aspect} composition, no readable text, no logos, no subtitles, "
            "no watermark, no border. NOT a painting or 2D illustration."
        ),
        "video_style": (
            "Modern 3D CGI animated feature, stylised-realistic character design, physically "
            "based rendering, ray-traced global illumination"
        ),
        "video_texture": (
            "physically based materials, subsurface skin, simulated cloth and groomed hair, "
            "ray-traced shadows and global illumination, cinematic depth of field"
        ),
    },
    "anime": {
        "sheet_noun": "modern Japanese 2D anime character model sheet",
        "sheet_technical": (
            "Use a solid white background, flat even lighting, centered layout, full body "
            "visible from head to toe in each main view, clean thin line art, 2-3 tone cel "
            "shading, consistent line weight and colour across every view, and clean "
            "professional model-sheet organization. The overall result should feel like a "
            "production model sheet for a TV anime series."
        ),
        "sheet_tail": (
            "Modern Japanese 2D anime, clean thin line art, solid cel shading, minimal "
            "gradients, no watermark, no extra props, no background environment, minimal "
            "or no text labels."
        ),
        "wardrobe_rule": "keep the costume design exactly as described, drawn with clean readable shapes",
        "wardrobe_default": "Dress them in a costume that fits the role, drawn with clean readable shapes.",
        "plate_opening": "Modern Japanese 2D anime background art, wide establishing shot of",
        "plate_materials": "Painted surfaces with clean shapes, soft cel-shaded shadows, a harmonious palette.",
        "plate_camera": (
            "Eye-level camera, wide-angle layout, balanced composition, clear spatial depth, "
            "atmospheric perspective, hand-painted anime background texture."
        ),
        "plate_tail": (
            "Modern Japanese 2D anime background art, {aspect} composition, no main "
            "characters, no readable text, no logos, no subtitles, no watermark."
        ),
        "frame_tail": (
            "Modern Japanese 2D anime film still, clean thin line art, solid cel shading, "
            "{aspect} composition, no readable text, no logos, no subtitles, no watermark, no border."
        ),
        "video_style": "Modern Japanese hand-drawn 2D anime, clean thin line art, solid cel shading, controlled compositing",
        "video_texture": "clean cel-shaded rendering, hand-drawn effects animation with impact frames, smears and speed lines, no photoreal textures",
    },
}


def _style(name: Optional[str]) -> dict[str, str]:
    from flowboard.services import film_styles
    if film_styles.is_preset(name):
        medium = film_styles.video_style(name)
        return {**STYLES[film_styles.base_style(name)], 'video_style': medium,
                'video_texture': medium,
                'frame_tail': medium + ' {aspect} composition; no text, watermark or border.'}
    return STYLES.get(str(name or "realistic"), STYLES["realistic"])


def style_from_rules(visual_style: Optional[str]) -> str:
    """Which preset a free-text target style means.

    The rules are prose a person can edit, so the board cannot store a preset
    key and hope it matches; it reads the words back instead. One place does
    it, because three places doing it slightly differently is how a board ends
    up generating anime sheets against 3D plates."""
    text = str(visual_style or "").lower()
    from flowboard.services import film_styles
    for key in film_styles.KEYS:
        if key in text:
            return key
    if "anime" in text or "2d" in text:
        return "anime"
    if "3d" in text or "cgi" in text or "pixar" in text or "render" in text:
        return "cg3d"
    return "realistic"

# Image models, cheapest-capable first. Kept in sync with the Flow studio's
# list by hand — a shared constant would drag the whole studio store into this
# demo for four strings. Two engines: Gemini runs through Atrium, Seedream
# through Avis, and the model id alone says which.
IMAGE_MODELS = (
    "gemini-3-pro-image",
    "gemini-3.1-flash-image",
    "gemini-2.5-flash-image",
    "dola-seedream-5-0-pro",
)
# Seedream by the owner's pipeline (2026-09-24): every sheet and plate of the
# X-Ray re-make that was approved came from it.
DEFAULT_IMAGE_MODEL = os.getenv("FLOWBOARD_IMAGE_MODEL", "dola-seedream-5-0-pro")
# Largest resolution each model actually delivers. Seedream caps near 2K
# (~4.6M px) whatever you ask for, so asking 4K there only wastes a round trip.
IMAGE_MODEL_MAX = {
    "gemini-3-pro-image": "4K",
    "gemini-3.1-flash-image": "4K",
    "gemini-2.5-flash-image": "2K",
    "dola-seedream-5-0-pro": "2K",
}
IMAGE_SIZES = ("1K", "2K", "4K")
# An environment plate is a set drawing, not a frame from the film: it wants
# width to show where things are in relation to each other, whatever ratio the
# film itself is cut in.
ENVIRONMENT_ASPECT = "21:9"
DEFAULT_IMAGE_SIZE = "2K"


def is_seedream(image_model: str) -> bool:
    return str(image_model).startswith("dola-seedream")


def capped_size(image_model: str, image_size: Optional[str]) -> str:
    """The size to actually ask for: what was wanted, or the model's ceiling."""
    wanted = (image_size or DEFAULT_IMAGE_SIZE).upper()
    if wanted not in IMAGE_SIZES:
        wanted = DEFAULT_IMAGE_SIZE
    ceiling = IMAGE_MODEL_MAX.get(image_model, "2K")
    order = {"1K": 1, "2K": 2, "4K": 3}
    return wanted if order[wanted] <= order[ceiling] else ceiling


# ─────────────────────────────── breakdown ────────────────────────────────

_BREAKDOWN_SYSTEM = """\
You are a film development lead breaking a raw premise into a production \
package for a short drama that will be generated shot by shot by an AI video \
model. Work like a studio: invent whatever the premise leaves out, and commit \
to specifics.

Return ONE JSON object and nothing else — no prose, no markdown fence.

{
  "title":     short film title, 1-3 words, tied to a concrete object or line
               in the story rather than to its theme,
  "logline":   one sentence, max 40 words,
  "runtime_seconds": integer,
  "characters": [{
    "key":     lowercase ascii slug, stable,
    "name":    full name,
    "role":    e.g. "lead", "antagonist", "witness",
    "summary": 1-2 sentences on who they are and what they want,
    "identity_anchor": one or two PHYSICAL marks that survive every state —
               a mole, a scar, freckles. This is what stops the image model
               drifting into a different face between states.
    "states":  [{
      "key":      lowercase ascii slug,
      "label":    e.g. "2022, age 26",
      "look":     face/build/hair at this point, and how it differs from the
                  other state WITHOUT changing the identity,
      "wardrobe": specific garments, fabrics, colours, footwear,
      "posture":  how they hold themselves and take up space
    }]
  }],
  "environments": [{
    "key":      lowercase ascii slug,
    "name":     short name,
    "summary":  what is in the room and what is the central element,
    "lighting": the light sources and how they mix,
    "mood":     the emotional register, stated as a contrast
                ("calm and private rather than clinical"),
    "lock":     what must stay identical if this place is revisited
  }],
  "sequences": [{
    "key":      lowercase ascii slug,
    "label":    e.g. "SEQ 01",
    "title":    short scene title,
    "duration_s": float seconds,
    "summary":  what happens, and what changes by the end of it,
    "beat":     the one dramatic turn this sequence exists to deliver,
    "environment_key": key from environments,
    "character_keys":  keys from characters
  }]
}

Rules that decide whether this works:
- A character's states are ONE person at different points. Never split a \
person into two characters because they aged or changed clothes.
- Sequence durations must sum to runtime_seconds.
- Give a character no more than three states, and the film no more than six \
environments — every extra one is a plate someone has to generate and check.
- Aim for 10-18 sequences. Break on dramatic turns, never on a word count.
- No background score anywhere. Sound is dialogue, breath, SFX and ambience \
only; music is added later in the edit.

Do NOT write individual shots here. Shots are cut per sequence, later, once \
the cast and places are approved.
"""

# Shots are cut one sequence at a time, for the same reason the film itself is
# generated one episode at a time: a whole film's shotlist in a single call is
# a five-minute generation that gets thrown away the moment a character note
# changes. Per sequence it is seconds, and a re-cut costs one sequence.
_SHOTS_SYSTEM = """\
You are a director cutting ONE sequence of a continuous short film into shots \
for an AI video model. You are given the cast, the location, the sequence, how \
the PREVIOUS sequence ended, and what the NEXT one must pick up.

This is not a standalone scene. It is the middle of a film already running.

The model reading your shots is literal and has no taste. It will not infer a \
performance from a summary, and it will overplay anything you leave vague. So \
write direction, not description: what the body does, in order, and what it \
must NOT do.

Return ONE JSON object and nothing else — no prose, no markdown fence.

{"shots": [{
  "n":        1-based integer within this sequence,
  "title":    SHORT TITLE IN CAPS naming the beat, e.g. "MAYA CUTS HER HAND",
  "duration_s": float seconds,
  "framing":  WIDE | MS | MCU | CU | ECU | OTS | INSERT | 2-SHOT,
  "lens_mm":  a RANGE as a string, e.g. "70-100" — never a single number,
  "camera":   the setup in one clause: angle, movement, depth of field,
              e.g. "low insert, static, shallow DOF" or "medium tracking backward",
  "action":   ARRAY of 1-4 short beats in the order they happen. One physical
              event per beat. Not a paragraph.
  "dialogue": [{"who": SPEAKER IN CAPS, "line": verbatim line}],
  "performance": ARRAY of 2-5 observable micro-actions, in order — the body,
              not the feeling. "small inhale", "jaw tightens", "eyes flick to
              the door and back", "does not cry". Never "she feels afraid".
  "avoid":    ARRAY of 1-3 things the model must NOT do in this shot. This is
              the most important field. Name the obvious wrong reading:
              "no cartoon villain", "not thrown at her — knocked off the table",
              "no theatrical shouting", "no romantic smile".
  "sfx":      ARRAY of discrete layered sounds, not one phrase:
              ["sleeve movement", "bottle slide", "glass impact on marble"],
  "edit_note": optional, when timing carries the beat: "hold 0.5-0.8s after the
              line", "cut immediately after the punchline, do not linger",
  "character_keys": keys of characters visible in this shot
}],
 "function": ARRAY of the things this sequence must establish for the audience,
             e.g. ["Ethan is extremely rich", "Maya is the new caregiver"],
 "raccord":  ARRAY of continuity locks for THIS sequence — screen direction,
             prop positions, anything that must not move between shots,
             e.g. ["ETHAN screen-left, MAYA screen-right", "whiskey table keeps
             its exact position"],
 "exit_state": "One sentence: where each character is, what they hold, what they
             now know, what physically just changed. The next sequence opens
             from this, so write it as fact."}

Continuity — what makes this a film and not a slideshow:
- OPEN FROM THE EXIT STATE. A held breath, a hand not yet removed, a decision \
just taken — the first shot carries it forward. Emotion does not reset.
- Do NOT re-establish a location already seen, and do not default to a wide \
opening. Open wide only when the place is new. Otherwise open on a face, a \
hand, or an object already in play.
- Carry a physical thread across the cut — something held, worn or being done.
- If time passed, SHOW how much in the first shot: light, a changed garment, a \
cleared table.
- END mid-decision, mid-movement or mid-look. Never tidily resolved.

Craft:
- Shot durations must sum to the sequence duration given.
- Dialogue runs at ~160 words per minute. Shorten the LINE to fit the shot, \
never stretch the shot.
- After two or three close-ups, break the run: an insert, an OTS, a profile, a \
wide, a detail of hands. A film is not a sequence of portraits.
- Every shot is ONE continuous camera setup. If the camera would have to cut, \
that is two shots.
- No background music anywhere. Sound is dialogue, breath, SFX and room tone.
"""


def _extract_json(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply.

    Models fence JSON, prefix it with "Here is...", or trail a closing remark
    even when told not to. Rather than fail the whole run on punctuation, take
    the outermost braces and parse that.
    """
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AutomationError("The model did not return JSON. Try running the breakdown again.")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AutomationError(f"The model returned malformed JSON ({exc.msg}). Try again.") from exc


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _as_strs(value: Any) -> list[str]:
    """Coerce a field that should be a list of lines but might arrive as one.

    Models drop back to a plain string under pressure, and a shotlist that
    silently loses its avoid-list is worse than one that never had it.
    """
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


async def build_breakdown(
    script: str,
    *,
    runtime_seconds: Optional[int] = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Raw premise → cast, environments and shotlist.

    ``runtime_seconds`` pins the length when the user has one in mind
    (``mode=fixed``); left out, the model derives one from the content.
    """
    premise = script.strip()
    if not premise:
        raise AutomationError("Write the premise first — there is nothing to break down yet.")

    ask = [f"PREMISE\n{premise}"]
    if runtime_seconds:
        ask.append(
            f"\nTOTAL RUNTIME\nExactly {runtime_seconds} seconds. Shot durations must sum to this."
        )
    else:
        ask.append(
            "\nTOTAL RUNTIME\nNot given — derive one from the content and say what you chose."
        )

    reply = await run_llm(
        "planner",
        "\n".join(ask),
        system_prompt=_BREAKDOWN_SYSTEM,
        timeout=timeout,
    )
    data = _extract_json(reply)

    characters = _as_list(data.get("characters"))
    environments = _as_list(data.get("environments"))
    sequences = _as_list(data.get("sequences"))
    if not characters and not environments:
        raise AutomationError("The breakdown came back empty. Try a longer premise.")

    return {
        "title": str(data.get("title") or "Untitled").strip(),
        "logline": str(data.get("logline") or "").strip(),
        "runtime_seconds": int(data.get("runtime_seconds") or runtime_seconds or 0),
        "characters": characters,
        "environments": environments,
        "sequences": sequences,
    }


async def build_shots(
    sequence: dict[str, Any],
    *,
    characters: list[dict[str, Any]],
    environments: list[dict[str, Any]],
    previous_exit: str = "",
    previous_label: str = "",
    next_summary: str = "",
    same_location: bool = False,
    timeout: float = 240.0,
) -> dict[str, Any]:
    """Cut one sequence into shots, joined to the ones either side of it.

    Returns ``{"shots": [...], "exit_state": "..."}``.

    The neighbours are the point. An earlier version passed only this
    sequence's own cast and location, on the theory that a model handed less
    context would not re-decide settled things. What it actually produced was
    fourteen self-contained scenes: every one opened on a fresh establishing
    wide, and a violation at the end of one sequence had evaporated by the
    start of the next. Continuity cannot be inferred from a summary — the exit
    state has to be handed over as fact, which is what ``exit_state`` is for.
    """

    used_chars = set(sequence.get("character_keys") or [])
    cast = [c for c in characters if c.get("key") in used_chars] or characters
    env = next(
        (e for e in environments if e.get("key") == sequence.get("environment_key")), None
    )

    ask = {
        "sequence": sequence,
        "characters": [
            {
                "key": c.get("key"),
                "name": c.get("name"),
                "role": c.get("role"),
                "summary": c.get("summary"),
            }
            for c in cast
        ],
        "environment": {"key": env.get("key"), "name": env.get("name")} if env else None,
        "previous_sequence": (
            {"label": previous_label, "ended_with": previous_exit} if previous_exit else None
        ),
        "next_sequence_needs": next_summary or None,
        # Told plainly rather than left for the model to work out from two
        # location names, because "do not re-establish" is the rule it breaks most.
        "location_already_seen": same_location,
    }

    reply = await run_llm(
        "planner",
        json.dumps(ask, ensure_ascii=False, indent=2),
        system_prompt=_SHOTS_SYSTEM,
        timeout=timeout,
    )
    data = _extract_json(reply)
    shots = _as_list(data.get("shots"))
    if not shots:
        raise AutomationError("No shots came back for this sequence. Try cutting it again.")
    return {
        "shots": shots,
        "function": _as_strs(data.get("function")),
        "raccord": _as_strs(data.get("raccord")),
        "exit_state": str(data.get("exit_state") or "").strip(),
    }


# ──────────────────────────── prompt assembly ─────────────────────────────


def _sentence(text: Any) -> str:
    """Trim a model-supplied fragment and give it a full stop."""
    out = str(text or "").strip().rstrip(".")
    return f"{out}." if out else ""


def _bullets(items: Any, limit: int = 12) -> str:
    """A design brief's list, as one readable clause per item."""
    out = []
    for item in (items or [])[:limit]:
        if isinstance(item, dict):
            piece = str(item.get("piece") or item.get("name") or "").strip()
            bits = [str(item.get(k) or "").strip() for k in ("material", "colour", "color", "detail")]
            body = ", ".join(b for b in bits if b)
            out.append(f"{piece}: {body}" if piece else body)
        elif str(item).strip():
            out.append(str(item).strip())
    return "; ".join(b for b in out if b)


def build_design_character_prompt(
    character: dict[str, Any],
    design: dict[str, Any],
    state: dict[str, Any],
    *,
    has_reference: bool,
    style: str = "realistic",
) -> str:
    """A model sheet written the way a working art brief is written.

    Short declarative lines under fixed headings, not paragraphs. Measured
    against the paragraph version on the same cast: a 2,000-character brief in
    this shape held the layout and the costume better than a 9,800-character one
    that said the same things in prose. Length was buying nothing; the headings
    and the terseness were doing the work.

    Two sections exist that description alone never produces:

    PRESENCE  — what the silhouette should COMMUNICATE. "Popular, untouchable,
                media-trained" directs a pose and a set of choices; a list of
                garments does not.
    DISTINCT FROM — a character defined against the cast around them. Two
                girls of the same age in the same school uniform converge
                unless one is told to stay softer and less severe than the
                other.
    """
    look = _style(style)
    name = str(character.get("name") or "the character").strip()
    role = str(character.get("role") or "character").strip()

    def section(head: str, body: Any) -> str:
        if isinstance(body, (list, tuple)):
            body = "\n".join(f"- {x}" for x in body if str(x).strip())
        body = str(body or "").strip()
        return f"{head}:\n{body}" if body else ""

    parts: list[str] = [
        f"Create a premium {look['sheet_noun']} for {name.upper()}, {role}."
    ]

    parts.append(section("STYLE", look["sheet_technical"] + (
        " The reference images are frames of this exact character from the film: "
        "reproduce that same face, build, hair and costume. Do not restyle them."
        if has_reference else "")))
    parts.append(section("FORMAT", FORMAT_BLOCK))
    parts.append(section("LAYOUT — 60/40", TURNAROUND_BLOCK))

    # Age leads, on its own line. Left to a paragraph it drowns, and a brief
    # that mentions freckles and peach fuzz turns a sixteen-year-old into a
    # child — measured, on this cast.
    parts.append(section("CHARACTER", "\n".join(filter(None, [
        _sentence(design.get("age_read")),
        _sentence(design.get("build")),
        _sentence(character.get("summary")),
    ]))))
    parts.append(section("FACE", " ".join(filter(None, [
        _sentence(design.get("face")) or _sentence(character.get("identity_anchor")),
        _sentence(design.get("expression_default")),
    ]))))
    parts.append(section("HAIR", _sentence(design.get("hair"))))

    costume = design.get("costume") or []
    worn = [c for c in costume if not _is_footwear(c) and not _is_carried(c)]
    shoes = [c for c in costume if _is_footwear(c)]
    # Anything slung over a shoulder is drawn once, at the front. A backpack
    # worn through a turnaround hides the back and the side — the two views the
    # turnaround exists to provide.
    carried = _lines(design.get("carried")) + [_costume_line(c) for c in costume if _is_carried(c)]
    parts.append(section("OUTFIT", [_costume_line(c) for c in worn]
                         or _sentence(state.get("wardrobe"))))
    parts.append(section("FOOTWEAR", [_costume_line(c) for c in shoes]))
    if carried or _lines(design.get("props")):
        parts.append(section(
            "CARRIED — FRONT VIEW ONLY",
            carried + _lines(design.get("props"))
            + ["Show these in the front full-body view only. The side and back "
               "views and every head study are empty-handed and unobstructed."]))
    parts.append(section("COLOUR", _bullets(design.get("palette"), 8)))

    # A second visible state is a panel, not an adjective: said inside the face
    # description it is simply ignored.
    if _sentence(design.get("signature_state")):
        parts.append(section("ABILITY DETAIL", (
            f"Normal state as described above. "
            f"{_sentence(design.get('signature_state'))} "
            "Add ONE small extra inset panel showing that state only. Keep the "
            "effect controlled and readable, never explosive, and do not let it "
            "spill into the other panels.")))

    parts.append(section("POSE", POSE_BLOCK))
    # What the design is FOR, not what it is made of.
    parts.append(section("PRESENCE", " ".join(filter(None, [
        f"The silhouette should communicate: {_sentence(design.get('silhouette'))}"
        if design.get("silhouette") else "",
        f"Read at a glance as {role}.",
    ]))))

    parts.append(section("RENDERING", " ".join(filter(None, [
        _sentence(design.get("render_notes")),
        look["sheet_tail"],
        "Consistent facial identity and proportions across every view.",
        MATERIAL_BLOCK,
    ]))))
    parts.append(section("NO", _lines(design.get("avoid")) + [
        "alternative costume variants",
        "rendered background or scenery",
        SHEET_EXCLUSIONS.split("EXCLUDE — ", 1)[-1].rstrip("."),
    ]))
    return "\n\n".join(x for x in parts if x.strip())


def build_design_environment_prompt(
    environment: dict[str, Any],
    design: dict[str, Any],
    *,
    aspect_ratio: str = "16:9",
    style: str = "realistic",
    has_reference: bool = False,
) -> str:
    """An establishing plate written from a design brief, section by section.

    Same shape as the character sheet for the same reason: a plate is a
    production document, and naming the sections is what stops the model
    averaging them into "a nice room". A location has no pose and no costume,
    so those sections are replaced by the ones a set drawing actually needs —
    what it is built from, how it is lit, what the air is doing, and what a
    camera would be able to see from where.
    """
    look = _style(style)
    name = str(environment.get("name") or "the location").strip()
    aspect = "vertical 9:16" if aspect_ratio == "9:16" else "cinematic 16:9"

    def block(head: str, body: str) -> str:
        body = " ".join(str(body).split())
        return f"{head}\n{body}" if body else ""

    parts: list[str] = []
    opening = f"{look['plate_opening']} {name}, empty — no characters, no crowds."
    if has_reference:
        opening += (
            " The reference images are frames of this exact place from the film: "
            "keep its architecture, materials, colour and light. Do not redesign it."
        )
    parts.append(opening)

    parts.append(block("THE PLACE", _sentence(design.get("architecture"))))
    parts.append(block("BUILT FROM", _bullets(design.get("materials"), 10)))
    parts.append(block("LIGHT", _sentence(design.get("light"))))
    parts.append(block("AIR", _sentence(design.get("atmosphere"))))
    parts.append(block("DRESSING", _bullets(design.get("set_dressing"), 8)))
    parts.append(block("DEPTH", _sentence(design.get("depth"))
                       + " Foreground, midground and background must each read separately."))
    parts.append(block("COLOUR", _bullets(design.get("palette"), 8)))
    parts.append(block("SURFACES", PLATE_MATERIAL_BLOCK))

    camera = _sentence(design.get("camera")) or look["plate_camera"]
    parts.append(block(
        "CAMERA AND RENDER",
        f"{camera} {look['plate_materials']} {_sentence(design.get('render_notes'))} "
        f"{look['plate_tail'].format(aspect=aspect)}",
    ))

    avoid = _lines(design.get("avoid"))
    parts.append(block(
        "EXCLUSIONS",
        "; ".join(avoid + [
            "no people, animals or silhouettes of either",
            "no readable text, signage, logos or watermarks",
            "no collage, split panels, insets or borders",
            "no tilted horizon, fisheye or heavy vignette",
        ]) + ".",
    ))
    return "\n\n".join(x for x in parts if x.strip())


def build_character_prompt(
    character: dict[str, Any],
    state: dict[str, Any],
    *,
    has_reference: bool,
    style: str = "realistic",
    design: Optional[dict[str, Any]] = None,
) -> str:
    """Assemble a turnaround-sheet prompt in the house four-block shape.

    ``has_reference`` switches the identity block between the two jobs a sheet
    can have: with no reference image this sheet IS the master face, so the
    block describes it from scratch; with one, the sheet inherits that face and
    the block's whole job is to stop the model redrawing it.
    """
    from flowboard.services import film_styles
    if film_styles.is_preset(style):
        return film_styles.sheet_prompt('character', {**character, **({'design': design} if design else {})},
                                        state=state, has_reference=has_reference, style=style)
    if design:
        return build_design_character_prompt(
            character, design, state, has_reference=has_reference, style=style
        )
    name = str(character.get("name") or "the character").strip()
    role = str(character.get("role") or "character").strip()
    anchor = _sentence(character.get("identity_anchor"))
    look = _sentence(state.get("look"))
    posture = _sentence(state.get("posture"))
    wardrobe = _sentence(state.get("wardrobe"))
    summary = _sentence(character.get("summary"))
    look_style = _style(style)
    noun = look_style["sheet_noun"]

    if has_reference:
        identity = (
            f"Create a {noun} for {role} based on Image 1. "
            f"Preserve the character's core identity and facial features exactly as in "
            f"Image 1 — the same person, the same bone structure, the same eyes. "
            f"{anchor} {look} {posture} It must read unmistakably as the same person as Image 1."
        )
    else:
        identity = (
            f"Create a {noun} for {role}, {name}. "
            f"{look} {anchor} {posture} {summary}"
        )

    wardrobe_block = (
        f"Dress them as follows, and {look_style['wardrobe_rule']}: {wardrobe}"
        if wardrobe
        else look_style["wardrobe_default"]
    )

    return "\n\n".join(
        [
            " ".join(identity.split()),
            TURNAROUND_BLOCK,
            " ".join(wardrobe_block.split()),
            f"{look_style['sheet_technical']} {look_style['sheet_tail']}",
        ]
    )


def build_environment_prompt(
    environment: dict[str, Any], *, aspect_ratio: str = "16:9", style: str = "realistic",
    design: Optional[dict[str, Any]] = None, has_reference: bool = False,
) -> str:
    """Assemble an establishing-plate prompt in the house shape."""
    from flowboard.services import film_styles
    if film_styles.is_preset(style):
        return film_styles.sheet_prompt('environment', {**environment, **({'design': design} if design else {})},
                                        has_reference=has_reference, style=style)
    if design:
        return build_design_environment_prompt(
            environment, design, aspect_ratio=aspect_ratio, style=style, has_reference=has_reference
        )
    look_style = _style(style)
    name = str(environment.get("name") or "the location").strip()
    summary = _sentence(environment.get("summary"))
    lighting = _sentence(environment.get("lighting"))
    mood = _sentence(environment.get("mood"))

    aspect = "vertical 9:16" if aspect_ratio == "9:16" else "cinematic 16:9"
    body = (
        f"{look_style['plate_opening']} {name}, no main characters present. "
        f"{summary} {lighting} {mood} "
        f"{look_style['plate_materials']} "
        f"{look_style['plate_camera']} {look_style['plate_tail'].format(aspect=aspect)}"
    )
    return " ".join(body.split())


# ────────────────────────────── image plates ──────────────────────────────


def _publish(png: bytes) -> Optional[str]:
    """Put one generated image somewhere with a URL, or give up.

    This does two jobs at once. Atrium pulls reference images over HTTP, so an
    identity portrait can only anchor the sheets below it if it lives at a
    public URL. And a board has to survive being reopened in another tab, which
    a data URL cannot do — pixels are far too big to keep in the saved board,
    so what gets saved is the link.

    Without R2 there is neither: plates still generate, but they vanish when
    the tab closes and every sheet grows its own face. The caller reports that
    rather than letting it be discovered later.
    """
    if not r2.is_configured():
        return None
    return _publish_bytes(png, "png")


def _publish_bytes(blob: bytes, ext: str) -> Optional[str]:
    """Upload one blob to R2 and hand back its public URL, or None."""
    if not r2.is_configured():
        return None
    tmp = Path(tempfile.gettempdir()) / f"automation-{uuid.uuid4().hex}.{ext}"
    try:
        tmp.write_bytes(blob)
        return r2.upload_file(tmp)
    except Exception as exc:  # noqa: BLE001 — a missing upload degrades, never fails the gen
        logger.warning("automation: upload failed (%s: %s)", type(exc).__name__, exc)
        return None
    finally:
        tmp.unlink(missing_ok=True)


async def _fetch_reference_bytes(urls: list[str]) -> list[bytes]:
    """Seedream takes reference images INLINE, not by URL — so a plate that
    chains from an R2 link has to be pulled back down first."""
    import httpx

    out: list[bytes] = []
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for url in urls:
            try:
                res = await client.get(url)
                res.raise_for_status()
                out.append(res.content)
            except Exception as exc:  # noqa: BLE001 — a missing ref is not fatal
                logger.warning("automation: could not fetch reference %s: %s", url, exc)
    return out


async def generate_plate(
    prompt: str,
    *,
    image_model: str = DEFAULT_IMAGE_MODEL,
    aspect_ratio: str = "16:9",
    reference_urls: Optional[list[str]] = None,
    variant_count: int = 1,
    image_size: Optional[str] = None,
    style: str = '',
    material_kind: str = '',
    style_version: str = '',
) -> list[dict[str, Any]]:
    """Run one plate through Atrium and give it a home.

    Every plate is published, not just the identity portraits it used to be:
    an image that exists only as a data URL in one tab is lost the moment that
    tab closes, and cannot be written into the saved board either. Published,
    the board stores a link — small enough to save, reachable from any tab, and
    usable as Atrium's Image 1.

    Each entry: ``url`` to show, ``reference_url`` to chain from (the same
    link, absent when R2 is not configured), and ``persisted`` so the caller
    can say plainly whether this picture will still be there tomorrow.
    """
    if not prompt.strip():
        raise AutomationError("This node has no prompt yet.")
    size = capped_size(image_model, image_size)
    seedream = is_seedream(image_model)
    from flowboard.services import film_styles
    preset_sheet = film_styles.is_preset(style) and material_kind in ('character', 'environment', 'prop', 'background_group')
    if preset_sheet:
        if style_version and style_version != film_styles.version(style):
            raise AutomationError('Style preset changed. Rebuild the material prompt before generation.')
        if not seedream:
            raise AutomationError('The approved film style presets use Seedream through Avis.')
        aspect_ratio = '16:9'

    if seedream and not avis_api.is_configured():
        raise AutomationError("Avis is not configured — set AVIS_API_KEY in .env.")
    if not seedream and not atrium_api.is_configured():
        raise AutomationError(
            "Atrium is not configured — set ATRIUM_CLIENT_ID and ATRIUM_CLIENT_SECRET in .env."
        )

    try:
        if seedream:
            refs = await _fetch_reference_bytes(reference_urls or [])
            if preset_sheet:
                if len(refs) != len(reference_urls or []):
                    raise AutomationError('A required identity/material reference could not be loaded; image slots were not shifted.')
                style_reference = film_styles.reference_path(material_kind, style)
                if style_reference is not None:
                    refs.insert(0, style_reference.read_bytes())
            images = await avis_api.generate_image_variants(
                prompt,
                refs or None,
                image_model=image_model,
                aspect_ratio=aspect_ratio,
                variant_count=variant_count,
                image_size=size,
            )
        else:
            images = await atrium_api.generate_image_variants(
                prompt,
                reference_urls or [],
                image_model=image_model,
                aspect_ratio=aspect_ratio,
                variant_count=variant_count,
                image_size=size,
            )
    except Exception as exc:  # noqa: BLE001 — surface the provider's own words
        raise AutomationError(f"{type(exc).__name__}: {exc}"[:300]) from exc

    out: list[dict[str, Any]] = []
    for img in images:
        url = await asyncio.to_thread(_publish, img)
        # Also land it in the media store. KYC identity assets are created from
        # a media_id with a local file — not from a URL — so a plate that is
        # never ingested can never lock a real face later.
        media_id = await asyncio.to_thread(_ingest_plate, img)
        out.append(
            {
                # No R2: hand back the pixels so the plate is at least visible
                # in this tab, and flag that it will not outlive it.
                "url": url or f"data:image/png;base64,{base64.b64encode(img).decode('ascii')}",
                "reference_url": url,
                "media_id": media_id,
                "persisted": url is not None,
            }
        )
    return out


def clip_filename(index: int, label: str, title: str) -> str:
    """A name that sorts into story order in any file browser.

    Zero-padded index FIRST, because that is the only part guaranteed to sort
    correctly — sequence labels go wrong at ten ("SEQ 10" sorts before
    "SEQ 02" in some tools) and titles sort alphabetically, which is meaningless
    for a film. The label and title ride along so a human can still read it.
    """
    def slug(text: str) -> str:
        out = unicodedata.normalize("NFKD", text or "")
        out = out.encode("ascii", "ignore").decode("ascii")
        out = re.sub(r"[^A-Za-z0-9]+", "-", out).strip("-")
        return out[:48] or "untitled"

    return f"{index:02d}_{slug(label)}_{slug(title)}.mp4"


async def bundle_clips(clips: list[dict[str, Any]]) -> Path:
    """Fetch every generated clip and zip it, in the order given.

    Order is the caller's: the board knows the running order and this does not
    second-guess it. Written to a temp file rather than memory — a film's worth
    of 720p runs to hundreds of megabytes and the request should not hold that
    twice.
    """
    import httpx
    import zipfile

    if not clips:
        raise AutomationError("Chưa có clip nào để tải.")

    out = Path(tempfile.gettempdir()) / f"clips-{uuid.uuid4().hex}.zip"
    written = 0
    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
            for i, clip in enumerate(clips, start=1):
                url = str(clip.get("url") or "")
                if not url or url.startswith("data:"):
                    continue
                name = clip_filename(i, str(clip.get("label") or ""), str(clip.get("title") or ""))
                try:
                    res = await client.get(url)
                    res.raise_for_status()
                except Exception as exc:  # noqa: BLE001 — skip one, keep the rest
                    logger.warning("automation: clip fetch failed %s (%s)", name, exc)
                    continue
                # ZIP_STORED, not DEFLATE: MP4 is already compressed, so
                # deflating it burns CPU on a whole film to save almost nothing.
                zf.writestr(name, res.content)
                written += 1

    if not written:
        out.unlink(missing_ok=True)
        raise AutomationError("Không tải được clip nào — kiểm tra lại link trên R2.")
    return out


def plate_filename(name: str, kind: str, taken: set[str]) -> str:
    """The subject's own name, as a filename, kept unique.

    A sheet is looked for by WHO it is, so the name leads — unlike a clip,
    which is looked for by where it falls in the film and therefore leads with
    its index. Diacritics are folded rather than dropped so "Dương Viêm"
    becomes "Duong-Viem" and not "ng-im".
    """
    out = unicodedata.normalize("NFKD", name or "")
    out = "".join(c for c in out if not unicodedata.combining(c))
    out = out.replace("đ", "d").replace("Đ", "D")
    out = re.sub(r"[^A-Za-z0-9]+", "-", out).strip("-")[:64] or "untitled"
    base = f"{'nhan-vat' if kind == 'character' else 'boi-canh'}/{out}"
    stem, n = base, 2
    while f"{stem}.png" in taken:           # two characters can share a name
        stem, n = f"{base}-{n}", n + 1
    taken.add(f"{stem}.png")
    return f"{stem}.png"


async def bundle_plates(entries: list[dict[str, Any]]) -> Path:
    """Fetch every sheet and plate and zip them under their subject's name.

    Foldered by kind so a designer opening the zip sees the cast and the
    locations apart, which is how they are reviewed.
    """
    import httpx
    import zipfile

    if not entries:
        raise AutomationError("Chưa có ảnh nào để tải.")

    out = Path(tempfile.gettempdir()) / f"plates-{uuid.uuid4().hex}.zip"
    taken: set[str] = set()
    written = 0
    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
            for entry in entries:
                url = str(entry.get("url") or "")
                if not url or url.startswith("data:"):
                    continue
                name = plate_filename(str(entry.get("name") or ""),
                                      str(entry.get("kind") or "character"), taken)
                try:
                    res = await client.get(url)
                    res.raise_for_status()
                except Exception as exc:  # noqa: BLE001 — skip one, keep the rest
                    logger.warning("automation: plate fetch failed %s (%s)", name, exc)
                    continue
                # PNG is already compressed; deflating 24 of them saves nothing.
                zf.writestr(name, res.content)
                written += 1

    if not written:
        out.unlink(missing_ok=True)
        raise AutomationError("Không tải được ảnh nào — kiểm tra lại link trên R2.")
    return out


async def ingest_published(urls: list[str]) -> dict[str, Optional[str]]:
    """Give already-published plates a media row, without regenerating them.

    Plates made before the pipeline ingested anything exist only as R2 links,
    and a KYC identity asset can only be built from a media row with a cached
    file. Rather than make someone pay to regenerate artwork that is already
    correct, fetch the bytes back and plant them. Returns url → media_id, with
    None for anything that could not be fetched.
    """
    import httpx

    out: dict[str, Optional[str]] = {}
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for url in urls:
            if not url or url.startswith("data:"):
                out[url] = None
                continue
            try:
                res = await client.get(url)
                res.raise_for_status()
                out[url] = await asyncio.to_thread(_ingest_plate, res.content)
            except Exception as exc:  # noqa: BLE001 — one bad URL must not sink the batch
                logger.warning("automation: re-ingest failed for %s (%s)", url, exc)
                out[url] = None
    return out


def _ingest_plate(png: bytes) -> Optional[str]:
    """Cache one plate in the media store and return its media_id."""
    from flowboard.services import media as media_service

    media_id = str(uuid.uuid4())
    try:
        ok = media_service.ingest_inline_bytes(media_id, png, kind="image", mime="image/png")
    except Exception as exc:  # noqa: BLE001 — a plate is still usable without it
        logger.warning("automation: media ingest failed (%s: %s)", type(exc).__name__, exc)
        return None
    return media_id if ok else None


def reference_chain_available() -> bool:
    """Whether an identity portrait can anchor the sheets derived from it."""
    return r2.is_configured()


# ─────────────────────────────── video ────────────────────────────────────
#
# One clip per SEQUENCE, not per shot. Seedance 2.5 will not go under 4s and is
# most stable under 20s, which is exactly the band our sequences already fall
# in — and a sequence generated whole keeps its own continuity instead of
# asking four separate clips to agree with each other. The shots inside it
# become the timestamped slices of the storyboard module.

VIDEO_MODEL_ID = "dreamina-seedance-2-5"
VIDEO_MIN_S, VIDEO_MAX_S = 4, 30
# The guide's own advice: past ~20s stability drops and takes a re-roll.
VIDEO_STABLE_S = 20


def video_duration_for(seconds: float) -> int:
    """Round a sequence's length UP to whole seconds Seedance will accept.

    Up, not to nearest: a re-made reference clip of 12.4s has its last shot
    ending at 12.4s, and a 12s generation would cut that shot off."""
    return max(VIDEO_MIN_S, min(VIDEO_MAX_S, math.ceil((seconds or 0) - 0.05)))


def _timecode(seconds: float) -> str:
    # Reference shots are often under a second; whole seconds would print a
    # 0.4s impact insert as "3s-3s".
    whole = round(seconds)
    return f"{whole}s" if abs(seconds - whole) < 0.05 else f"{seconds:.1f}s"


_SHOT_REF = re.compile(r"\bshots?\s+(\d+)", re.IGNORECASE)


def _renumber_shots(text: str, local_of_source: dict[int, int]) -> str:
    """Point a shot cross-reference at this clip's numbering, or drop it.

    The adapter writes its timing notes against the reference video's own
    numbering — "line continues into shot 24" — but a clip renumbers its shots
    from 1. On the 0919 board that left 8 references to shots that do not exist
    inside their clip: an 11-shot clip told to cut to shot 35. A contradiction
    is worse than a missing note, so a reference outside this clip is cut.
    """
    def swap(m: re.Match[str]) -> str:
        local = local_of_source.get(int(m.group(1)))
        return f"shot {local}" if local else ""

    return " ".join(_SHOT_REF.sub(swap, text).split()).replace(" ,", ",").replace(" .", ".")


def _lines(value: Any) -> list[str]:
    """Read a field that is a list now but used to be a single string.

    Boards cut before the richer schema landed still hold `action` as prose and
    `performance` as one sentence. Those are worth rendering, not discarding.
    """
    return _as_strs(value)


def _joined(value: Any, sep: str = " ") -> str:
    return sep.join(_lines(value))


def build_keyframe_prompt(
    shot: dict[str, Any],
    *,
    which: str,                       # "start" | "end"
    characters: list[dict[str, Any]],
    environment: Optional[dict[str, Any]],
    style: str = "realistic",
    aspect_ratio: str = "16:9",
) -> str:
    """One still: the first or last frame of a clip, to be generated as an image.

    Seedance can be given a first and a last frame and asked to travel between
    them, and that is the strongest consistency handle there is — the two ends
    of every clip are pictures WE control, made from the same cast sheets and
    the same location plate. What it costs is the reference images on that
    call: keyframe interpolation and multi-reference are different modes and
    the provider refuses to mix them, so identity has to be carried by the
    frames themselves. Hence this prompt is written like a photograph brief,
    not like a scene: one moment, one camera, everybody's face nailed down.
    """
    look = _style(style)
    beats = _lines(shot.get("action"))
    moment = (beats[0] if beats else "") if which == "start" else (beats[-1] if beats else "")
    framing = str(shot.get("framing") or "").strip()
    camera = str(shot.get("camera") or "").strip()
    lens = str(shot.get("lens_mm") or "").strip()

    who = " ".join(
        f"{c.get('name')}: {_sentence(c.get('identity_anchor'))} {_sentence(c.get('look'))} "
        f"{_sentence(c.get('wardrobe'))}".strip()
        for c in characters
        if c.get("name")
    )
    where = str((environment or {}).get("name") or "the location").strip()
    env_bits = " ".join(
        filter(None, [
            _sentence((environment or {}).get("summary")),
            _sentence((environment or {}).get("lighting")),
        ])
    )
    aspect = "vertical 9:16" if aspect_ratio == "9:16" else "cinematic 16:9"

    body = (
        f"A single still frame from a film — the {'opening' if which == 'start' else 'closing'} "
        f"frame of the shot. {_sentence(moment)} "
        + (f"Framing: {framing}. " if framing else "")
        + (f"Camera: {camera}. " if camera else "")
        + (f"Lens {lens}mm. " if lens else "")
        + (f"Characters in frame — {who} Keep every face, hairstyle and costume exactly as in the "
           f"reference images. " if who else "")
        + f"Location: {where}. {env_bits} "
        f"{look['frame_tail'].format(aspect=aspect)} "
        "One frozen moment, not a poster: no collage, no split screen, no text overlay."
    )
    return " ".join(body.split())


_FRAMING_WORDS = {
    "WIDE": "Wide shot", "EWS": "Extreme wide shot", "WS": "Wide shot",
    "MWS": "Medium-wide shot", "MS": "Medium shot", "MCU": "Medium close-up",
    "CU": "Close-up", "ECU": "Extreme close-up", "OTS": "Over-the-shoulder shot",
    "POV": "Point-of-view shot", "INSERT": "Insert shot",
}

# Per-look closing rules for the supplement. The positive half says what the
# picture IS, the negative half names the neighbouring looks a model drifts to.
_SUPPLEMENT_LOOK = {
    "anime": (
        "Clean thin line art. Strict 2-tone solid cel shading on characters. Minimal "
        "gradients on characters. Controlled detail. Animate primarily ON THREES for "
        "character acting, with smoother motion only for fast action and camera moves.",
        "No photorealism. No 3D/CGI look. No noisy textures. No heavy gradients. No AI artifacts.",
    ),
    "cg3d": (
        "Physically based materials, strand-based hair, clean subsurface skin and "
        "controlled specular highlights. Weighty, readable character animation.",
        "No 2D anime cel shading. No painterly look. No live-action photography. No AI artifacts.",
    ),
    "realistic": (
        "Motivated practical lighting, natural skin and realistic physical textures.",
        "No animation look. No CGI look. No AI artifacts.",
    ),
}

_SCHOOL_WORDS = ("student", "school", "grade", "senior", "junior", "freshman",
                 "sophomore", "pupil", "classmate", "high school", "academy")


# A garment coming OFF, or an intimate garment being SHOWN — not a garment being
# worn. Written against the X-Ray board's own text, where the loose version of
# this ("strip", "bra") also caught "ceiling strip lights", "striped ties",
# "nude-polish nails" and a party guest's "bra top", none of which is anything.
_GARMENT = r"(?:clothes|clothing|jacket|shirt|blouse|uniform|dress|skirt|top|camisole|blazer|sweater)"
_INTIMATE = r"(?:bra|bras|lingerie|underwear|panties|knickers)"
_UNDRESSING = re.compile(
    r"\bundress\w*"
    rf"|\bstrip\w*\s+(?:away|off)\s+(?:her|his|their|the)?\s*(?:\w+\s+){{0,2}}{_GARMENT}"
    rf"|\b{_GARMENT}\s+(?:\w+\s+)?(?:dissolv\w*|vanish\w*|disappear\w*|melt\w*|tears?\s+into|breaks?\s+into|falls?\s+away|fades?\s+away)"
    rf"|\b(?:reveal\w*|leaving|showing|exposing|exposes?)\s+(?:\w+\s+){{0,6}}{_INTIMATE}\b",
    re.IGNORECASE,
)


def school_age(characters: list[dict[str, Any]], environment: Optional[dict[str, Any]]) -> bool:
    """Whether this cast reads as school-age, from everything said about it.

    Not the role alone: the board sends a character as its name, look and
    design, and on X-Ray the one word that says "school" in that payload is the
    costume — "charcoal navy school blazer", "school uniform tie".
    """
    bits: list[Any] = [(environment or {}).get("name")]
    for c in characters:
        design = c.get("design") or {}
        bits += [c.get("role"), c.get("summary"), c.get("look"), c.get("wardrobe"),
                 c.get("identity_anchor"), design.get("age_read")]
        bits += [f"{p.get('piece', '')} {p.get('detail', '')}" for p in design.get("costume") or []
                 if isinstance(p, dict)]
    about = " ".join(str(x) for x in bits if x).lower()
    return any(re.search(rf"\b{re.escape(w)}", about) for w in _SCHOOL_WORDS)


def unsafe_shots(shots: list[dict[str, Any]], characters: list[dict[str, Any]],
                 environment: Optional[dict[str, Any]]) -> list[tuple[int, str]]:
    """Shots that take clothes off a school-age cast, as (shot number, the words).

    Refused rather than softened. A supplement line saying "no underwear" cannot
    rescue a shot whose own action says the camisole dissolves to show it — the
    model is handed two orders and follows the specific one.
    """
    if not school_age(characters, environment):
        return []
    hits: list[tuple[int, str]] = []
    for i, shot in enumerate(shots, start=1):
        text = " ".join(
            " ".join(v) if isinstance(v, list) else str(v or "")
            for v in (shot.get(k) for k in ("title", "action", "performance", "framing_note"))
        )
        m = _UNDRESSING.search(text)
        if m:
            hits.append((i, m.group(0)))
    return hits


_ACTING_LINES_MAX = 3
_NEGATIVES_MAX = 1
_SFX_MAX = 3
_CAMERA_NEGATIVE = re.compile(r"\b(?:camera|reframe|rack focus|push in|drift|dolly|zoom)\b", re.IGNORECASE)
_VOICE_SFX = re.compile(r"\b(?:line|lines|voice|dialogue|says|speaks|question|reply)\b", re.IGNORECASE)
# A line whose whole job is to say someone talks: "Theo speaks, holding her
# eyeline", "He says the line while …". The dialogue block carries that. Only
# when the speaking is the sentence's own verb: "Theo lifts the green box as he
# speaks" is an action with a clause attached, and the box is what matters.
_SPEAKING_ONLY = re.compile(
    r"^(?:\w+\s+){0,2}?(?:speaks?|says?|asks?|repl(?:y|ies)|answers?|delivers?|mouths?)\b",
    re.IGNORECASE,
)


# How fast Seedance 2.5 actually speaks English, measured on the user's own
# ten-shot office clip: 56 words in 25.1 seconds of speech, 2.2 words a second
# on average, never faster than 3.4. Every line there took 1.5 to 3 times the
# slot it was written into, and all eight still landed only because that clip
# was generated eleven seconds longer than its timeline. Sized to the lines, a
# timeline needs no such slack — and slack is what made it say a line twice.
SPEECH_WPS = 2.8
_LINE_FLOOR_S = 0.7      # "Wait." still takes a breath
_BREAK_S = 0.25          # a full stop or an ellipsis inside a line
_TAIL_S = 0.3            # the beat after the last word, before the cut


def speech_seconds(line: str) -> float:
    """Roughly how long a line takes to say at the model's own pace."""
    text = str(line or "")
    words = len(re.findall(r"[A-Za-z0-9À-ɏ'’]+", text))
    breaks = len(re.findall(r"[.!?…—]+(?=\s*\S)", text.strip()))
    return max(_LINE_FLOOR_S, words / SPEECH_WPS + breaks * _BREAK_S)


def fit_shots_to_lines(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies of the shots, each stretched to fit what is said in it.

    A shot keeps its measured length whenever the line fits. When it does not,
    the shot grows rather than the line being cut short: a reference actor can
    fire nine words into 2.7 seconds, and a generated one does not.
    """
    fitted = []
    for shot in shots:
        said = [d for d in shot.get("dialogue") or [] if d.get("line")]
        need = sum(speech_seconds(d["line"]) for d in said) + _TAIL_S if said else 0.0
        dur = float(shot.get("duration_s") or 0)
        fitted.append({**shot, "duration_s": round(max(dur, need), 2)})
    return fitted


def clip_seconds(sequence: dict[str, Any], shots: list[dict[str, Any]]) -> int:
    """The generation length: the fitted timeline's end, in whole seconds."""
    timeline = sum(float(s.get("duration_s") or 0) for s in fit_shots_to_lines(shots))
    return video_duration_for(max(timeline, 0.0) or (sequence.get("duration_s") or 0))


def _camera_line(camera: Any) -> str:
    """The camera's position and movement, without the lens notes after it.

    "Over-the-shoulder, eye-level, static, shallow depth of field (~T2.2)
    holding Theo's eyes sharp while …" becomes "Over-the-shoulder, eye-level,
    static." — the part a model can act on.
    """
    text = re.sub(r"\s*\([^)]*\)", "", str(camera or "")).split(";")[0]
    bits = [b.strip() for b in text.split(",") if b.strip()]
    return _sentence(", ".join(bits[:3])) if bits else ""


def _first_clause(text: Any, names: tuple[str, ...] = ()) -> str:
    """Who is where in the frame — the part before the background is described.

    With names, the first clause that mentions one of them: an over-the-shoulder
    note often opens on the foreground shoulder ("Out-of-focus grey suit shoulder
    fills the left third") and only then says where the character is.
    """
    clauses = [c.strip() for c in str(text or "").split(";") if c.strip()]
    if not clauses:
        return ""
    for c in clauses:
        if any(re.search(rf"\b{re.escape(n)}\b", c, re.IGNORECASE) for n in names):
            return _sentence(c)
    return _sentence(clauses[0])


def _mmss(seconds: float) -> str:
    """00:02.4 — one decimal where the edit needs it, whole seconds otherwise."""
    minutes, rest = divmod(max(seconds, 0.0), 60)
    whole = round(rest)
    body = f"{whole:02d}" if abs(rest - whole) < 0.05 else f"{rest:04.1f}"
    return f"{int(minutes):02d}:{body}"


def _preserve_line(character: dict[str, Any]) -> str:
    """What a HARD reference must keep, in a handful of nouns — not a biography.

    The working prompts this format comes from name four to six things
    ("white hair, glasses, black suit, white gloves") and stop. A paragraph of
    face anatomy here is what the sheet is for; the clip only has to be told
    which parts of the sheet are non-negotiable.
    """
    design = character.get("design") or {}
    bits: list[str] = []
    hair = _sentence(design.get("hair")).split(";")[0].split(",")[0].rstrip(".")
    if hair:
        bits.append(hair[0].lower() + hair[1:] if hair[:1].isupper() else hair)
    for piece in (design.get("costume") or [])[:3]:
        name = str(piece.get("piece") or "").strip()
        colour = str(piece.get("colour") or "").split(",")[0].split(" and ")[0].strip()
        if name:
            bits.append(f"{colour.lower()} {name.lower()}".strip() if colour else name.lower())
    return ", ".join(bits)


def build_video_prompt(
    sequence: dict[str, Any],
    shots: list[dict[str, Any]],
    *,
    characters: list[dict[str, Any]],
    environment: Optional[dict[str, Any]],
    style: Optional[str] = None,
    look: str = "realistic",
    aspect_ratio: Optional[str] = None,
) -> str:
    """Assemble one sequence into a Seedance 2.5 clip prompt.

    The shape is the one the user's hand-written prompts use, and it is here
    because it was measured, not because it reads well. On a ten-shot office
    scene with three speakers and eight lines, that shape delivered all eight
    lines, all eight from the right mouth, and all ten shots in order — against
    0 of 4 lines from the previous template on a comparable clip.

      [CREATIVES DESCRIPTION]  every @imageN with a ROLE and a MODE
      FORMAT                   aspect, and no score
      [BLOCKING]               who stands where, when two or more share a scene
      [ONE-SENTENCE SUMMARY]
      [SPECIFIC TIMELINE]      one block per shot, its line inside it
      [OVERALL SUPPLEMENT]     every standing rule, LAST

    Three things the old template did the other way round, and why they moved:

    * The location reference is REFERENCE ONLY. It used to say "lock the
      location to its image — same layout", which is an instruction to paste
      the plate behind every shot; the working prompts forbid exactly that and
      ask for a new angle per shot.
    * Each spoken line sits once, inside its own shot, under the speaker's name
      in capitals. Pulling them into a block up front and repeating them in the
      shot put every line in the prompt twice.
    * The standing rules close the prompt instead of opening it, so the first
      thing the model reads after the cast is the story.
    """
    shots = fit_shots_to_lines(shots)
    total = clip_seconds(sequence, shots)
    look_style = _style(look)
    look_key = look if look in _SUPPLEMENT_LOOK else "realistic"
    style = style or look_style["video_style"]
    cut = sequence.get("editing") == "cut"
    where = str((environment or {}).get("name") or "the location").strip()

    spoken_anywhere = any(
        line.get("line") for shot in shots for line in shot.get("dialogue") or []
    )
    named = [c for c in characters if c.get("ref_label")]
    first_names = tuple(str(c.get("name") or "").split()[0] for c in characters if c.get("name"))
    # Which key a caps speaker name belongs to, to tell an on-screen line from
    # one heard over someone else's shot.
    key_of = {str(c.get("name") or "").split()[0].upper(): c.get("key")
              for c in characters if c.get("name") and c.get("key")}

    parts: list[str] = []

    # ── [CREATIVES DESCRIPTION] ──
    creatives: list[str] = []
    for c in named:
        name = str(c.get("name") or "the character").strip()
        keep = _preserve_line(c)
        creatives.append(
            f"{c['ref_label']} — {name.upper()}, HARD CHARACTER REFERENCE. Preserve "
            f"{name}'s identity, face"
            + (f", {keep}" if keep else "")
            + ", body proportions and costume exactly as in the image."
            + (f" {_sentence((c.get('design') or {}).get('identity_lock'))}"
               if (c.get("design") or {}).get("identity_lock") else "")
        )
    if environment and environment.get("ref_label"):
        label = environment["ref_label"]
        creatives.append(
            f"{label} — {where.upper()}, ENVIRONMENT REFERENCE ONLY. Use only for the "
            "location's architectural language, materials, lighting mood and atmosphere. "
            f"DO NOT reproduce {label} as an exact background, exact composition or exact "
            "camera angle. Every shot must use a newly generated angle of this place."
        )
    # Anyone the shots name who has no reference is generated fresh, and the
    # working prompts guard the one thing that goes wrong with a crowd.
    if any(not (shot.get("character_keys") or []) for shot in shots):
        creatives.append(
            "Every other person on screen is newly generated and must look clearly "
            "different from everyone else: unique face, hairstyle, silhouette and "
            "demeanour. No clones."
        )
    if creatives:
        parts.append("[CREATIVES DESCRIPTION]\n\n" + "\n\n".join(creatives))

    # ── FORMAT ──
    fmt = [f"FORMAT: {aspect_ratio}."] if aspect_ratio else []
    fmt += ["NO MUSIC.", "NO BGM.",
            "Dialogue and SFX only." if spoken_anywhere else "Natural ambience and SFX only."]
    parts.append("\n".join(fmt))

    # ── [BLOCKING] ──
    # Taken from a shot that shows the characters side by side — a two-shot or
    # wider — and only from a clause naming at least two of them. The first
    # shot with two keys is often an over-the-shoulder, whose note is about a
    # shoulder, not about where anyone stands.
    if len(named) >= 2:
        def names_in(text: str) -> int:
            return sum(bool(re.search(rf"\b{re.escape(n)}\b", text, re.IGNORECASE)) for n in first_names)
        placing = ""
        for shot in sorted(shots, key=lambda s: str(s.get("framing") or "").upper() in ("OTS", "CU", "ECU", "MCU", "INSERT")):
            if len(shot.get("character_keys") or []) < 2:
                continue
            clause = next((c for c in str(shot.get("framing_note") or "").split(";") if names_in(c) >= 2), "")
            if clause:
                placing = _sentence(clause.strip())
                break
        if placing:
            parts.append(
                "[BLOCKING]\n\n"
                f"* {placing}\n"
                "* Maintain this spatial relationship throughout the scene unless the "
                "shot angle naturally obscures part of it."
            )

    # ── [ONE-SENTENCE SUMMARY] ──
    summary = str(sequence.get("summary") or "").split(" Beats:")[0].strip()
    summary = _sentence(sequence.get("goal") or summary or sequence.get("title"))
    parts.append(f"[ONE-SENTENCE SUMMARY]\n\n{summary} Set in {where}.")

    # ── [SPECIFIC TIMELINE] ──
    local_of_source: dict[int, int] = {}
    for i, s in enumerate(shots, start=1):
        for src in (s.get("source_shots") or [s.get("source_shot")]):
            if str(src or "").isdigit():
                local_of_source[int(src)] = i

    timeline: list[str] = []
    at = 0.0
    for i, shot in enumerate(shots, start=1):
        dur = float(shot.get("duration_s") or 0)
        start, end = at, at + dur
        at = end
        block: list[str] = [f"[SHOT {i} — {_mmss(start)}–{_mmss(end)}]"]

        # The working prompts spend 150-500 characters on a shot: a size and
        # an angle, who is where, two or three sentences of acting, the line,
        # one delivery note. The adaptation writes three times that, and the
        # surplus is not harmless — it is what buries the line. So each field
        # is cut back to the part those prompts actually use.
        framing = str(shot.get("framing") or "").upper()
        size = _FRAMING_WORDS.get(framing, f"{framing} shot" if framing else "")
        head = " ".join(x for x in [f"{size}." if size else "", _camera_line(shot.get("camera"))] if x)
        if head:
            block.append(head)
        placement = _first_clause(shot.get("framing_note"), first_names)
        if placement:
            block.append(placement)
        lines_here = [d for d in shot.get("dialogue") or [] if d.get("line")]
        # Action before performance, because blocking outranks nuance; and a
        # line that only announces the speech goes, as the NAME: block below
        # already says it.
        acting, seen = [], set()
        for b in _lines(shot.get("action")) + _lines(shot.get("performance")):
            line = _sentence(b)
            key = line.lower()
            if not line or key in seen:
                continue
            if lines_here and _SPEAKING_ONLY.search(line):
                continue
            seen.add(key)
            acting.append(line)
        if acting:
            block.append("\n".join(acting[:_ACTING_LINES_MAX]))

        in_frame = set(shot.get("character_keys") or [])
        for d in lines_here:
            who = str(d.get("who") or "SPEAKER").strip().upper()
            # Screenplay marks, so the voice is not put in the wrong mouth: a
            # line heard over another character's shot is off-screen, and the
            # second half of a line split at a cut says it carries on.
            marks = []
            if d.get("cont"):
                marks.append("continuing")
            key = key_of.get(who.split()[0]) if who else None
            if key and in_frame and key not in in_frame:
                marks.append("off-screen")
            label = f"{who} ({', '.join(marks)})" if marks else who
            block.append(f'{label}:\n"{str(d["line"]).strip()}"')
        if lines_here and shot.get("edit_note"):
            note = _renumber_shots(_sentence(shot["edit_note"]), local_of_source)
            if note:
                block.append(note)
        if spoken_anywhere and not lines_here:
            block.append("There is NO dialogue in this shot.")

        # One negative, as the working prompts use them ("He does NOT …"). The
        # camera ones go first, since a static head already says it.
        avoid = sorted(_lines(shot.get("avoid")), key=lambda a: bool(_CAMERA_NEGATIVE.search(a)))
        if avoid:
            block.append("\n".join(
                a if a.lower().startswith(("do not", "no ", "never")) else f"Do NOT {a[0].lower()}{a[1:]}"
                for a in (_sentence(x).rstrip(".") + "." for x in avoid[:_NEGATIVES_MAX])))
        # A sound that is the line itself ("his low urgent line") is not an effect.
        sfx = [x for x in _lines(shot.get("sfx")) if not _VOICE_SFX.search(x)]
        if sfx:
            block.append("SFX: " + " → ".join(sfx[:_SFX_MAX]) + ".")
        timeline.append("\n\n".join(block))
    if timeline:
        parts.append("[SPECIFIC TIMELINE]\n\n" + "\n\n".join(timeline))

    # ── [OVERALL SUPPLEMENT] ──
    positive, negative = _SUPPLEMENT_LOOK[look_key]
    sup: list[str] = [f"Total runtime: about {total} seconds.", f"{style}.", positive]
    sup.append(
        "Characters must blend seamlessly into the environment. The location's lighting "
        "must visibly affect the characters, with clean light and shadow shapes on faces "
        "and clothing."
    )
    moods = [
        f"* {str(c.get('name')).strip()}: {_sentence((c.get('design') or {}).get('expression_default')).rstrip('.')}"
        for c in named if (c.get("design") or {}).get("expression_default")
    ]
    if moods:
        sup.append("Performance style:\n" + "\n".join(moods))
    sup.append(
        f"Maintain one continuous geography of {where} across every shot."
        + (" Cut exactly at the timestamps above. Do not add, remove or reorder shots."
           if cut else " One continuous scene with no fast cuts.")
    )
    if named:
        sup.append("Every character keeps the same face, hair and costume in every shot.")
    # Mirrors the working prompts' own guards. A line in the supplement cannot
    # rescue a shot whose action contradicts it — those are refused before
    # this point — but it keeps the rest of the clip on the right side of it.
    if school_age(characters, environment):
        sup.append(
            "The characters are school-age. No nudity, no underwear, no clothing removal "
            "and no sexualised framing anywhere in the clip."
        )
    sup.append(
        ("Dialogue and SFX only." if spoken_anywhere else "Natural ambience and SFX only.")
        + "\nNO MUSIC.\nNO BGM.\nNO subtitles.\nNO captions.\nNO text overlays."
    )
    sup.append(
        "Only the specified English dialogue. No additional dialogue."
        if spoken_anywhere else "NO dialogue."
    )
    sup.append(negative)
    parts.append("[OVERALL SUPPLEMENT]\n\n" + "\n\n".join(sup))

    return "\n\n".join(p for p in parts if p.strip())


# Seedance is ByteDance's model, and it follows Chinese better than English —
# measured, not assumed. The same CLIP 02, the same five references, the same
# KYC path, the same 19 seconds, six written lines:
#
#   English, 29,792 chars .... 1 line spoken
#   English, 1,071 chars ..... 5 lines spoken
#   Chinese, 8,652 chars ..... 4 lines spoken
#   Chinese, 309 chars ....... 5 lines spoken, and evenly paced
#
# Length is one lever; language is a second, independent one. Translating the
# SAME prompt took it from 1 line to 4 while keeping all ten cuts, so the two
# stack. Note the character count is not the mechanism: on Claude's tokenizer
# the Chinese prompt is 4% smaller in tokens and 71% smaller in characters.
#
# The STAGING is what turns Chinese. The spoken lines stay in the language they
# were written in — they are audio the clip performs, not instruction the model
# reads — so they are lifted out before translation and put back after.
PROMPT_LANGUAGES = ("en", "zh")
_TRANSLATE_MODEL = "claude-sonnet-5"
_TRANSLATE_SYSTEM = """\
You translate AI video-generation prompts from English into Simplified Chinese.

Keep EVERY instruction, shot heading, timecode and @imageN marker exactly as it \
is — @image3 stays @image3, "Shot 4: 6s-8.5s" keeps its number and its times. \
Keep the bracketed section headings, translating only the words inside them.

Passages of the form ⟦12⟧ are SPOKEN LINES and CHARACTER NAMES that have been \
removed for safe keeping. Copy every one of them through unchanged, digits and \
brackets intact, in the same place in the sentence. Never translate one, never \
renumber one, never merge two, and never invent one that was not there.

Where the English refers to one of those characters by description rather than \
by name — "the warlord", "the sect leader" — keep it a description. Do NOT \
promote it into a Chinese title or name of its own; the cast is named only by \
the markers.

Be as concise as Chinese naturally allows, but drop nothing. Return only the \
translated prompt — no preamble, no fence."""

# What the audience HEARS and what it is CALLED stay in the adaptation's own
# language; only the staging around them turns Chinese. A spoken line is
# performed audio, not instruction, and a name the translator renders as 魔尊 is
# a name the model then speaks in Chinese — which is exactly what came back the
# first time. Asking the translator to leave them alone does not hold over a
# 20,000 character prompt; lifting them out and putting them back does.
_QUOTED_LINE = re.compile(r'"([^"\n]{1,400})"')
# A model writing Chinese re-punctuates as it goes, and the marker comes back
# wearing whichever bracket the surrounding text uses. Accept them all rather
# than lose a whole translation to a glyph swap.
_PLACEHOLDER = re.compile(r'["“「『]?[⟦【〔「［\[]\s*(\d+)\s*[⟧】〕」］\]]["”」』]?')


async def translate_prompt(
    prompt: str, language: str = "zh", *, keep: Sequence[str] = ()
) -> str:
    """Render a built prompt in the language Seedance answers best in.

    ``keep`` is the proper nouns — cast and locations — that must survive
    untranslated wherever they appear, spoken or written.

    Falls back to the English prompt rather than failing the clip: a prompt in
    the wrong language still generates, an exception does not.
    """
    if language not in PROMPT_LANGUAGES:
        raise AutomationError(f"Unknown prompt language {language!r}.")
    if language == "en" or not prompt.strip():
        return prompt
    from flowboard.services import avis_text

    spoken: list[str] = []

    def hide(m: re.Match[str]) -> str:
        spoken.append(m.group(1))
        return f"⟦{len(spoken) - 1}⟧"

    masked = _QUOTED_LINE.sub(hide, prompt)
    # Everything masked from here on is a bare name, and goes back bare — only
    # the spoken lines above carry their quotes.
    n_quoted = len(spoken)
    # Longest first: "Ma Tôn" is a substring of nothing here, but "Bách Hoa
    # Cung" inside "Cung Chủ Bách Hoa Cung" would be, and masking the short one
    # first would leave the long one half-replaced.
    # Case-insensitively: the adaptation writes a speaker's name in caps
    # (`DƯƠNG VIÊM says…`) while the bible holds it in title case, and an exact
    # match leaves the shouted one unprotected — which is how half the cast
    # still came back as 杨炎 after the names were supposedly kept.
    for name in sorted({n.strip() for n in keep if n and n.strip()}, key=len, reverse=True):
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        if not pattern.search(masked):
            continue
        # Each spelling keeps its own slot, so CAPS goes back as CAPS.
        seen_forms: dict[str, int] = {}

        def stash(m: re.Match[str]) -> str:
            form = m.group(0)
            if form not in seen_forms:
                spoken.append(form)
                seen_forms[form] = len(spoken) - 1
            return f"⟦{seen_forms[form]}⟧"

        masked = pattern.sub(stash, masked)
    # A 30,000-character storyboard translates to more than the 16k cap allows,
    # and a reply cut short loses exactly the tail where most of the dialogue
    # lives. Ask for room proportional to the prompt.
    budget = max(16000, min(60000, len(masked) // 2 + 4000))

    def restore(text: str) -> tuple[str, list[int], list[str]]:
        seen: set[int] = set()

        def show(m: re.Match[str]) -> str:
            i = int(m.group(1))
            if i >= len(spoken):
                return ""
            seen.add(i)
            return f'"{spoken[i]}"' if i < n_quoted else spoken[i]

        out = _PLACEHOLDER.sub(show, text)
        return (out,
                [i for i in range(n_quoted) if i not in seen],
                [spoken[i] for i in range(n_quoted, len(spoken)) if i not in seen])

    # Losing the markers is not a length problem and not a content problem — the
    # same prompt that came back without them translates cleanly on the next
    # attempt. Measured on the 0919 board: 2 of 16 clips lost every marker, and
    # both succeeded when re-sent unchanged. So ask twice before giving up.
    for attempt in (1, 2, 3):
        if attempt > 1:
            # An empty body is what this gateway returns when it is shedding
            # load, and eight 25,000-character translations in flight will make
            # it do that. Retrying in the same breath just gets shed again.
            await asyncio.sleep(4.0 * (attempt - 1) ** 2)
        try:
            out = await avis_text.complete(
                _TRANSLATE_MODEL,
                [{"role": "system", "content": _TRANSLATE_SYSTEM},
                 {"role": "user", "content": masked}],
                max_tokens=budget,
            )
        except Exception as exc:  # noqa: BLE001 — a clip in English beats no clip
            logger.warning("automation: prompt translation failed, sending English (%s)", exc)
            return prompt
        zh = out.text.strip()
        # Avis answers an over-long generation with an EMPTY body rather than an
        # error, so a short reply is indistinguishable from a refusal here — and
        # it is just as likely to come back whole on a second ask.
        if out.finish_reason == "length" or len(zh) < len(prompt) // 6:
            logger.warning(
                "automation: translation came back short on attempt %d (%d → %d chars, finish=%s)",
                attempt, len(prompt), len(zh), out.finish_reason or "-",
            )
            continue
        if not spoken:
            return zh
        zh, lost_lines, lost_names = restore(zh)
        if not lost_lines:
            if lost_names:
                logger.warning("automation: translation dropped names %s", lost_names)
            return zh
        logger.warning(
            "automation: translation lost %d of %d spoken lines on attempt %d",
            len(lost_lines), n_quoted, attempt,
        )
    # A line the translator dropped would be a line nobody ever speaks, and the
    # clip would come back silent with nothing to point at. English staging with
    # every line present beats Chinese staging with half of them gone.
    logger.warning("automation: translation did not come back usable, sending English")
    return prompt


async def prepare_clip(
    prompt: str,
    *,
    reference_urls: list[str],
    duration_seconds: int,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
    unmoderated: bool = True,
    kyc_media_ids: Optional[list[str]] = None,
    project_id: Optional[str] = None,
    first_frame_url: str = "",
    last_frame_url: str = "",
    previous_clip_url: str = "",
    chain: str = "extend",
) -> dict[str, Any]:
    """Send one sequence to Seedance 2.5 and wait for the clip.

    Reference ORDER is the contract: the provider binds ``@imageN`` to the Nth
    reference block, so the list handed in here must already be in the order
    the prompt's labels assume. Getting that wrong puts one character's face on
    another, which is why the caller builds both together.

    ``unmoderated`` routes through the B2B path — the moderated endpoint
    rejects photoreal people outright ("input image may contain real person"),
    and every cast sheet here is one.

    ``kyc_media_ids`` switches to the person-driven path: EVERY reference —
    cast and location alike — is turned into an Avis identity asset, in the
    same order, so ``@imageN`` keeps pointing at what the prompt says it does.
    That is what makes photoreal people acceptable to the model, and it is the
    same thing the Studio's own "Real person (KYC)" checkbox does.
    """
    from flowboard.services.video import registry as video_registry

    # Three ways to keep a clip consistent, and the provider refuses to mix any
    # two of them. Probed live against the B2B endpoint on 2026-09-18:
    #
    #   keyframes  we generate the first and last frame and it travels between
    #              them. Strongest — but a photoreal frame is REFUSED ("input
    #              image may contain real person"), even unmoderated, and it
    #              cannot be combined with a KYC identity ("first/last frame
    #              content cannot be mixed with reference media content").
    #              So: drawn styles only.
    #   chain      the previous clip goes in as a reference video, as an
    #              "extend" (this clip continues that one, ratio must be
    #              "adaptive") or a "reference" (match its look). A video of
    #              real people IS accepted, which is what makes this the
    #              live-action answer.
    #   references cast sheets and a location plate — KYC turns each into an
    #              identity asset, which is what gets photoreal people through.
    keyframes = bool(first_frame_url)
    chained = bool(previous_clip_url) and not keyframes
    if not keyframes and not chained and not reference_urls and not kyc_media_ids:
        raise AutomationError(
            "Generate the character sheets and the environment plate first — "
            "a clip with no references has nothing to keep consistent."
        )

    video_registry.register_defaults()
    try:
        provider = video_registry.get_video_provider(VIDEO_MODEL_ID)
    except Exception as exc:  # noqa: BLE001 — unregistered/misconfigured model
        raise AutomationError(f"Seedance is not available: {exc}"[:200]) from exc

    kyc_asset_ids: list[str] = []
    if kyc_media_ids and not keyframes and not chained:
        from flowboard.services.video import avis as avis_video

        for media_id in kyc_media_ids:
            try:
                kyc_asset_ids.append(
                    await avis_video.ensure_kyc_asset(
                        media_id, "Image", project_id=project_id, unmoderated=unmoderated
                    )
                )
            except Exception as exc:  # noqa: BLE001 — name the plate that failed
                raise AutomationError(
                    f"KYC asset failed for {media_id}: {type(exc).__name__}: {exc}"[:250]
                ) from exc

    params: dict[str, Any] = {
        "motion_prompt": prompt,
        "duration_seconds": duration_seconds,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "generate_audio": True,
        "content_filter_disabled": unmoderated,
    }
    if keyframes:
        # i2v: the frames ARE the input. References, KYC assets and the omni
        # hint all belong to the other mode and would be dropped (or would drop
        # the last frame) if sent alongside.
        params["first_frame_url"] = first_frame_url
        if last_frame_url:
            params["last_frame_url"] = last_frame_url
    elif chained:
        # The previous clip is the only input: its cast, light and grade are
        # what this one has to match, and sending cast sheets beside it would
        # put the request back under the real-person image check.
        params["reference_videos"] = [previous_clip_url]
        params["omni_reference_task_type"] = "extend" if chain == "extend" else "reference"
        if chain == "extend":
            # Extension takes its shape from the video it continues; any other
            # ratio is refused outright.
            params["aspect_ratio"] = "adaptive"
    else:
        # r2v: every input is a reference, none is a first frame. The cast and
        # the room are things to match, not the opening image of the shot.
        # Under KYC the provider ignores these entirely — see the docstring.
        params["reference_images"] = list(reference_urls)
        params["omni_reference_task_type"] = "reference"
        if kyc_asset_ids:
            params["kyc_image_asset_ids"] = kyc_asset_ids

    return params


async def generate_clip(prompt: str, **kwargs) -> dict[str, Any]:
    from flowboard.services.video import registry as video_registry
    params = await prepare_clip(prompt, **kwargs)
    provider = video_registry.get_video_provider(VIDEO_MODEL_ID)
    try:
        submitted, polled = await provider.run_to_completion(params)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — surface the provider's own words
        raise AutomationError(f"{type(exc).__name__}: {exc}"[:300]) from exc

    if polled.get("status") != "succeeded":
        raise AutomationError(
            f"Seedance returned {polled.get('status')!r}"
            + (f": {polled['error']}" if polled.get("error") else ".")
        )

    return await publish_clip(submitted, polled)


async def publish_clip(submitted: dict, polled: dict) -> dict[str, Any]:
    # The provider's own contract: `video_bytes` is authoritative and
    # `video_url` is a signed link that expires shortly. Re-host the bytes so
    # the board still plays the clip tomorrow, and only fall back to the
    # short-lived link when there is nowhere to put them.
    blob = polled.get("video_bytes")
    url = await asyncio.to_thread(_publish_bytes, blob, "mp4") if blob else None
    persisted = url is not None
    if not url:
        url = polled.get("video_url")
    if not url:
        raise AutomationError("Seedance finished but returned no video.")

    return {
        "url": url,
        "persisted": persisted,
        "job_id": submitted.get("external_job_id"),
        "warnings": list(submitted.get("warnings") or []),
    }
