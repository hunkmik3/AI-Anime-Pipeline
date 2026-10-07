"""Turn "a woman with silver hair" into something worth generating from.

The cast pass names people and says roughly what they look like, because that
is all it needs to map shots. A REFERENCE SHEET needs far more than that: what
the face is actually shaped like, how the hair is cut, what the costume is made
of, where the trims and the wear are, what the palette is. A sheet generated
from one line comes back generic, and generic is exactly what a pre-production
reference must not be.

So this pass looks at the real keyframes — the frames that person or place
actually appears in — and writes a design brief from them. It is the only pass
that shows the image model the subject rather than describing it, and it is the
difference between "a warrior in gold armour" and armour with a shoulder plate,
a lamellar skirt and a specific green-gold patina.

The brief is stored on the cast entry as ``design`` and is what the sheet
prompt is built from; the keyframes themselves are then ALSO passed to the
image model as references, so the sheet matches the film instead of merely
agreeing with its description.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from flowboard.services import avis_text
from flowboard.services.video_analyzer import adapt as adapt_mod

logger = logging.getLogger(__name__)

# Who writes the brief. The owner's pipeline puts GPT-6 Astra here (it reads
# frames too); Gemini, which wrote every brief before 2026-09-24, is the fallback.
DESIGN_MODEL = os.getenv("FLOWBOARD_DESIGN_MODEL", "gpt-6-astra")
DESIGN_FALLBACK = os.getenv("FLOWBOARD_DESIGN_FALLBACK", "gemini-3-8-flash")
MAX_FRAMES = 6

_SOURCE_STATE_RULES = """

SOURCE STATE AND REFERENCE DESIGN HAVE DIFFERENT ROLES:
observed_states are the supplied per-shot source observations. They control known
visibility, position, holder, hand, contents, object state and transitions for those
shots. Preserve explicit unknowns; an omitted detail is not proof of absence.
The design brief describes identity, construction, materials, palette and rendering,
not new per-shot events or state assignments. Do not reinterpret an unknown source
state as a known state because a sampled frame appears to suggest it. Do not add
shot numbers, timestamps, blocking or custody directives to the aesthetic brief.
Any reference_variants describe alternative sheet views/configurations of the SAME
asset, only where supported by its construction and supplied observations. They do
not say which variant is active in a filmed shot, require a transition, or establish
previously unknown contents, mechanisms or a lid angle. A reference-sheet pose or
variant never overrides an observed shot state. Compatible unresolved geometry may
remain unspecified; do not invent a more revealing view to settle it.
"""

_CHARACTER_SYSTEM = """You are a character designer writing the reference sheet brief for ONE character.

You get frames of them from the film, the target style, and what the analysis \
already knows. Look at the frames: every answer must be something visible in \
them, not an invention. Where a detail is never shown (the back of a costume, \
the shoes), design it so it FOLLOWS from what is shown, and say so in "inferred".

Return ONE JSON object and nothing else:
{
  "age_read": "how old they must READ on screen, as one short line: e.g. \"17, a senior — not a child and not an adult\"",
  "face": "shape, cheekbones, jaw, eye shape and colour, brows, nose, mouth, skin tone and finish — SHORT declarative lines, one fact each, 5-7 of them",
  "hair": "cut, length, parting, texture, colour and any gradient, how it is dressed or bound",
  "build": "height and proportions relative to a normal adult, posture, how they carry weight",
  "costume": [
    {"piece": "e.g. shoulder plate / under-robe / belt",
     "material": "e.g. hammered gold, waxed linen, lacquered leather",
     "colour": "specific, not 'dark'",
     "detail": "trim, embroidery, rivets, pattern, wear, how it fastens"}
  ],
  "palette": ["4-6 colours that define them, named precisely (e.g. 'oxidised teal', 'pale gold')"],
  "props": ["carried or worn objects, described the same way"],
  "silhouette": "what makes them readable as a black shape at a distance",
  "signature_state": "" or "a second visible state the sheet must ALSO show — a power activating, a transformation, a mask coming off — described in one line, or empty if they have none",
  "carried": ["bags, cases, instruments — things HELD or SLUNG rather than worn. These are drawn in the front view only and never in the turnaround, because a backpack hides the back and the side, which is what a turnaround exists to show"],
  "expression_default": "the face they wear at rest in this film",
  "render_notes": "how the target style should treat their materials, skin and hair",
  "inferred": ["anything you designed rather than saw"],
  "avoid": ["3-5 things a generator must NOT do with this character"]
}

Rules:
- Be concrete. "ornate armour" is worthless; "lamellar skirt of overlapping \
gold plates over a teal under-robe, each plate rimmed in dark patina" is a brief.
- CHOOSE, do not inventory. A brief that records every freckle, pore and lip \
crack reads as a forensic report and generates a different person each time \
because nothing in it is load-bearing. Pick the 3-5 details that make them \
recognisable across two different images and drop the rest. If a detail would \
not survive being seen at half scale, it does not belong.
- Write in short declarative lines, not paragraphs. One fact per line.
- Nothing worn should hide the body in a turnaround: bags and cases go in \
"carried", not in "costume".
- The neutral identity turnaround keeps both hands empty. Carried objects belong
  to scene actions, not permanent anatomy or wardrobe. Never infer a book/folder
  from a partly hidden phone or physical photograph; cross-check observed_states.
- Use wider body views for lower garments and footwear. A close-up crop cannot
  justify a skirt when wider source views show trousers. Do not invent a costume
  change or merge different outfits into one look.
- Do not change who they are. This is the same person the film shows, described \
properly — not a redesign.
- Costume: 4-10 pieces, head to feet, including what holds it together.
- Never mention a camera, a shot or a background: this is the character alone."""

_ENVIRONMENT_SYSTEM = """You are a production designer writing the reference brief for ONE location.

You get frames of the place from the film, the target style, and what the \
analysis already knows. Everything must be visible in the frames; where the \
frames only show a corner, extend it so it follows, and say so in "inferred".
Respect the location's physical function and scale. Do not decorate a confined
elevator cabin with lounge chairs, tables or other furniture unless clearly seen
in its own source frames. Keep circulation space and door clearance usable.
Separate an elevator cabin from an adjoining lobby; their furniture is not shared.

Return ONE JSON object and nothing else:
{
  "architecture": "what the place IS — structure, scale, how it is built, what a person entering would see",
  "materials": ["surface by surface: stone, metal, fabric, vegetation, with finish and wear"],
  "light": "sources, direction, colour temperature, time of day, how shadows fall",
  "atmosphere": "haze, dust, smoke, weather, particles — the air itself",
  "set_dressing": ["objects that make it this place and not a generic one"],
  "palette": ["4-6 precise colours"],
  "depth": "foreground, midground and background layers a plate should show",
  "camera": "the vantage and lens a wide establishing plate of it should use",
  "render_notes": "how the target style should treat its materials and light",
  "inferred": ["anything you designed rather than saw"],
  "avoid": ["3-5 things a generator must NOT put in this plate"]
}

Rules:
- Name real materials and real light. "Atmospheric" is not a material.
- No characters: this is the place, empty.
- The brief must let two different plates of this location match."""


def _frame_parts(work_dir: Path, frames: list[str]) -> list[dict]:
    parts: list[dict] = []
    for rel in frames[:MAX_FRAMES]:
        path = work_dir / rel
        if path.is_file():
            parts.append(avis_text.image_part(path))
    return parts


async def _design(system: str, known: dict, frames: list[dict], stats: adapt_mod.TextStats,
                  *, label: str, need_frames: bool = True) -> dict:
    # A redesign has nothing to look at on purpose — see "designing away from it".
    if need_frames and not frames:
        raise ValueError(f"{label}: no keyframes to design from")
    content = [avis_text.text_part(json.dumps(known, ensure_ascii=False))] + frames
    content.append(avis_text.text_part("Write the brief for this subject. JSON only."))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    last: Exception | None = None
    for model in dict.fromkeys(m for m in (DESIGN_MODEL, DESIGN_FALLBACK) if m):
        try:
            completion = await avis_text.complete(model, messages, temperature=0.3,
                                                  max_tokens=adapt_mod.MAX_TOKENS)
            stats.add(completion)
            data = avis_text.extract_json(completion.text)
        except avis_text.AvisTextError as exc:
            # Refused, filtered, unparseable: the next model gets the same frames.
            logger.warning("design %s: %s gave no brief (%s)", label, model, exc)
            last = exc
            continue
        if isinstance(data, dict):
            stats.answered_by[model] = stats.answered_by.get(model, 0) + 1
            data.setdefault("written_by", model)
            return data
        last = ValueError(f"{label}: design brief was not an object")
    raise ValueError(f"{label}: no design brief ({last})")


# ─────────────────────────── designing away from it ────────────────────────
#
# The pass above exists to RESEMBLE the reference. Sometimes the point is the
# opposite: keep the story and the shot list, throw the look away. Then the
# frames are not evidence to follow but the thing to depart from, so they are
# not sent at all — the brief is written from what the character DOES and from
# a stated art direction, and the sheet is generated without the film's frames
# as references (``use_frames=false``) or the reference would creep back in.

_REIMAGINE_RULES = """

YOU ARE REDESIGNING. The subject keeps its name, its role and everything it \
does in the story; its APPEARANCE is yours to invent inside the world brief \
below, and it must NOT resemble the original.

The world brief governs everything visible: materials, silhouette, palette, \
architecture, technology level, ornament. Do not carry over the original's \
costume, hair, colours or building style. If the description you were handed \
names a robe, a topknot or a sect hall, those are the things to replace, not \
to keep.

Design from FUNCTION: what this person does in the story tells you what they \
would wear in THIS world; what happens in this location tells you what it is \
built for. Keep the dramatic reading — who is powerful, who is wounded, what \
is sacred, what is ruined — and change everything else.

Put every invented detail in "inferred"; here that is most of it."""

_CHARACTER_REIMAGINE = _CHARACTER_SYSTEM.replace(
    "- Do not change who they are. This is the same person the film shows, described \\\nproperly — not a redesign.",
    "- Keep who they ARE — their role, rank and temperament. Change how they look.",
) + _REIMAGINE_RULES
_ENVIRONMENT_REIMAGINE = _ENVIRONMENT_SYSTEM + _REIMAGINE_RULES


async def design_character(entry: dict, work_dir: Path, rules: dict,
                           stats: adapt_mod.TextStats, *, world: str = "") -> dict:
    known = {
        "name": entry.get("name"),
        "role": entry.get("role"),
        "summary": entry.get("summary"),
        "identity_anchor": entry.get("identity_anchor"),
        "seen_as": entry.get("looks_like"),
        "states": entry.get("states"),
        "observed_states": entry.get("observed_states", []),
        "target_style": rules.get("visual_style"),
    }
    system=_CHARACTER_REIMAGINE if world else _CHARACTER_SYSTEM
    if entry.get('target_appearance'):
        from flowboard.services.target_casting import POLICY
        known['target_appearance']=entry['target_appearance']
        system+='\nEXPLICIT OWNER CASTING EXCEPTION\n'+POLICY
    if world:
        known["world_brief"] = world
        known.pop("identity_anchor", None)   # the old face is not evidence here
        known.pop("seen_as", None)
    return await _design(
        system,
        known,
        [] if world else _frame_parts(work_dir, entry.get("frames") or []),
        stats, label=str(entry.get("key")), need_frames=not world,
    )


async def design_environment(entry: dict, work_dir: Path, rules: dict,
                             stats: adapt_mod.TextStats, *, world: str = "") -> dict:
    known = {
        "name": entry.get("name"),
        "summary": entry.get("summary"),
        "lighting": entry.get("lighting"),
        "mood": entry.get("mood"),
        "lock": entry.get("lock"),
        "settings_seen": entry.get("settings"),
        "observed_states": entry.get("observed_states", []),
        "target_style": rules.get("visual_style"),
    }
    if world:
        known["world_brief"] = world
        known.pop("settings_seen", None)
    return await _design(
        (_ENVIRONMENT_REIMAGINE if world else _ENVIRONMENT_SYSTEM) + _SOURCE_STATE_RULES,
        known,
        [] if world else _frame_parts(work_dir, entry.get("frames") or []),
        stats, label=str(entry.get("key")), need_frames=not world,
    )


async def design_asset(entry: dict, work_dir: Path, rules: dict,
                       stats: adapt_mod.TextStats, *, world: str = "") -> dict:
    """Describe a recurring group or story prop from its own source evidence."""
    system = """Write a production reference brief for ONE background group or prop.
Return JSON: {"description":"...", "identity_details":[], "materials":[],
"members":[], "reference_variants":[], "dependencies":[], "inferred":[], "avoid":[]}.
Use the supplied source frames and inventory. Preserve the exact group membership,
distinguishing clothes/identities, prop geometry, container contents and any people
depicted in photographs. Do not invent faces, counts, text or unseen details.
For uncertain features say unknown. A crowd is an ensemble of distinct people,
not a single new character. Dependencies must reuse supplied asset ids.
Do not change who holds a prop or where a group stands: those are shot facts,
not a design decision. Describe appearance in the requested visual style.""" + _SOURCE_STATE_RULES
    known = {"asset": {key: value for key, value in entry.items() if key != 'observed_states'},
             "observed_states": entry.get('observed_states', []),
             "target_style": rules.get("visual_style")}
    if world:
        known["world_brief"] = world
        system += _REIMAGINE_RULES
    return await _design(system, known,
                         [] if world else _frame_parts(work_dir, entry.get("frames") or []),
                         stats, label=str(entry.get("key")), need_frames=not world)
