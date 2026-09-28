"""Bring a shot list written from the reference up to date with a redesigned cast.

Shots are adapted from the reference's own frames, and the cast is redesigned
after that — so the shots go on describing the actors' costumes. On the X-Ray
board, 1,142 mentions across 48 of 49 clips: "her light blue satin mini dress
and her pearl necklace", "Sienna's dark wavy hair", "Theo's grey stained
blazer", when Sienna's sheet is a platinum blunt bob in an ivory houndstooth
blazer. A clip prompt that says HARD CHARACTER REFERENCE in its first paragraph
and "pale blue satin shoulder" in its fifth hands the model two costumes.

The pass edits only what contradicts the new look, returns only the fields it
changed, and never sees a line of dialogue to change. The words it replaced are
kept beside the adaptation, so a conform can be undone.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

from flowboard.services.video_analyzer import adapt as adapt_mod

logger = logging.getLogger(__name__)

# The text a clip prompt is built from. Dialogue is deliberately absent: a
# line is the one thing this pass must never touch.
FIELDS = ("title", "camera", "framing_note", "lighting", "action", "performance",
          "avoid", "sfx", "vfx", "edit_note")
_LIST_FIELDS = {"action", "performance", "avoid", "sfx"}
BATCH = 12

_SYSTEM = """You update a film's shot list after its cast was redesigned.

The shots were written from a reference video, so they still describe what the \
reference's actors wore. Each character now has a NEW look, fixed by a character \
sheet the video model is given as an image. Any words in a shot that describe the \
OLD look contradict that sheet and must go.

You get the cast — for each character the new look ("now") and the old one \
("was") — and a batch of shots.

Change, and change only:
1. Words describing a cast member's clothing, hair, jewellery, make-up or worn \
accessories — in EVERY field, lighting and camera notes included ("the key \
catches the plaid tie", "focus on the near pearl of the necklace"). A person the \
shot does not name is still the cast member whose old look it describes. Delete the description and keep the rest of the sentence working \
("Sienna's dark wavy hair and pale blue satin shoulder occupy the left third" → \
"Sienna's shoulder occupies the left third"). When an action physically touches \
the item (a hand on a sleeve, tucking hair behind an ear, straightening a tie), \
keep the action and name the item plainly or as the new look has it ("her sleeve", \
"his navy tie").
2. Sounds that only exist because of the old costume ("pearl click", "satin \
rustle", "sequins shimmer") — make them neutral ("fabric rustle") or delete them.
3. Instructions to put text on screen — title cards, name captions, subtitles, \
burned-in words. The clip is generated with no text at all; delete them.

Never:
- add a description of anyone's look. The sheet carries it; the shot says what \
happens.
- change the camera, the framing, the blocking, the order or timing of actions, \
the lighting, or any name.
- remove a prop the story uses — a box someone holds, a phone, a watch that \
matters. A carried object stays unless the new look says otherwise.
- touch people who are not in the cast list (extras, crowds, bodyguards): their \
descriptions stay as they are.

Return ONE JSON array with an object only for shots you changed:
[{"shot": <int as given>, "changes": {"<field>": <the whole new value, same type \
as given — a list stays a list>}}]
Leave out every field you did not change, and every shot with nothing to change. \
An empty array is a correct answer."""


def _now_line(design: dict) -> dict:
    """The redesigned look, in the few nouns the pass needs to check against."""
    hair = str(design.get("hair") or "").split(".")[0].strip()
    wears = []
    for piece in design.get("costume") or []:
        name = str(piece.get("piece") or "").strip()
        colour = str(piece.get("colour") or "").strip()
        if name:
            wears.append(f"{name} — {colour}" if colour else name)
    props = design.get("props") or design.get("carried") or []
    if isinstance(props, str):
        props = [props]
    return {"hair": hair, "wears": wears, "carries": [str(p) for p in props][:4]}


def _was_line(character: dict) -> str:
    """The reference actor's look, as the cast reading described it."""
    bits = [str(character.get("looks_like") or "")]
    bits += [str(st.get("wardrobe") or "") for st in character.get("states") or []]
    bits.append(str(character.get("identity_anchor") or ""))
    return " / ".join(b.strip() for b in bits if b.strip())[:900]


def cast_sheet(cast: dict) -> dict[str, dict]:
    """Key → what the pass is told about that character."""
    sheet: dict[str, dict] = {}
    for c in cast.get("characters") or []:
        design = c.get("design") or {}
        if not design or not c.get("key"):
            continue
        sheet[c["key"]] = {"name": c.get("name"), "now": _now_line(design), "was": _was_line(c)}
    return sheet


# What a costume is made of — garments, fabrics, finishes, jewellery, hair.
# A look description also says "holds", "head" and "bodyguards"; only these
# words point at something a character wears.
_COSTUME = (
    "satin", "silk", "velvet", "lace", "denim", "leather", "sequin", "glitter", "chenille",
    "knit", "tweed", "plaid", "tartan", "stain", "grime", "jewel", "pearl", "necklace",
    "earring", "bracelet", "hoop", "choker", "pendant", "brooch", "halter", "strapless",
    "cutout", "spaghetti", "bodice", "gown", "dress", "skirt", "blazer", "jacket", "lapel",
    "necktie", "shirt", "blouse", "camisole", "cardigan", "sweater", "hoodie", "vest",
    "coat", "suit", "trouser", "jeans", "heel", "boot", "sneaker", "sandal", "loafer",
    "headband", "scarf", "beanie", "glasses", "aviator", "sunglass", "wavy", "curly",
    "braid", "ponytail", "bald", "buzz", "clutch", "sleeve", "collar",
)


def old_look_words(sheet: dict[str, dict]) -> set[str]:
    """Words only the reference actors' looks use — "satin", "plaid", "stained".

    A shot containing one is describing an old costume even when the shot map
    places nobody in it and no name is written.
    """
    out: set[str] = set()
    # Per character: Theo's old plaid tie is still Theo's old tie when someone
    # else's new outfit happens to be plaid.
    for v in sheet.values():
        now = set(re.findall(r"[a-z]{4,}", json.dumps(v.get("now"), ensure_ascii=False).lower()))
        was = re.findall(r"[a-z]{4,}", str(v.get("was") or "").lower())
        out |= {w for w in was if w not in now and w.startswith(_COSTUME)}
    return out


def suspect_shots(adaptation: dict, cast: dict) -> set[int]:
    """Shots whose text still uses an old look's words."""
    words = old_look_words(cast_sheet(cast))
    out = set()
    for n, shot in (adaptation.get("shots") or {}).items():
        text = json.dumps(_shot_payload(int(n), shot, []), ensure_ascii=False).lower()
        if words & set(re.findall(r"[a-z]{4,}", text)):
            out.add(int(n))
    return out


def _shot_payload(n: int, adapted: dict, who: list[str]) -> dict:
    row: dict[str, Any] = {"shot": n, "in_frame": who}
    for f in FIELDS:
        v = adapted.get(f)
        if v not in (None, "", []):
            row[f] = v
    return row


def _clean(change: dict, before: dict) -> dict:
    """Keep a change only when it is a real one, of the right type, to a known field."""
    out: dict[str, Any] = {}
    for f, v in (change or {}).items():
        if f not in FIELDS:
            continue
        if f in _LIST_FIELDS:
            if not isinstance(v, list):
                continue
            v = [str(x).strip() for x in v if str(x).strip()]
        elif v is not None and not isinstance(v, str):
            continue
        if v != before.get(f):
            out[f] = v
    return out


async def conform_shots(
    adaptation: dict,
    cast: dict,
    stats: adapt_mod.TextStats,
    *,
    only: Optional[set[int]] = None,
    on_progress=None,
    failed: Optional[list[int]] = None,
) -> dict[int, dict]:
    """{shot number: {field: new value}} for every field the new cast contradicts.

    Shots whose batch got no answer are appended to ``failed`` — "nothing to
    change" and "the gateway never replied" must not look the same.
    """
    sheet = cast_sheet(cast)
    per_shot = cast.get("shots") or {}
    adapted = adaptation.get("shots") or {}
    firsts = {k: str(v.get("name") or "").split()[0] for k, v in sheet.items() if v.get("name")}

    def who_in(n: int) -> list[str]:
        """Mapped to the shot, or named in its text. The shot map misses people
        — on X-Ray 87 shots of Theo carried no key while their text said "Theo
        Lambert's grey stained suit shoulder"."""
        keys = [k for k in (per_shot.get(str(n)) or {}).get("character_keys") or [] if k in sheet]
        text = json.dumps(_shot_payload(n, adapted[str(n)], []), ensure_ascii=False)
        keys += [k for k, first in firsts.items()
                 if k not in keys and first and re.search(rf"\b{re.escape(first)}\b", text)]
        return keys

    numbers = sorted(int(n) for n in adapted if only is None or int(n) in only)
    if only is None:
        # Asked for everything: every shot a redesigned character is in, is
        # named in, or is recognisable in by an old costume's words.
        suspects = suspect_shots(adaptation, cast)
        numbers = [n for n in numbers if who_in(n) or n in suspects]
    batches = [numbers[i:i + BATCH] for i in range(0, len(numbers), BATCH)]
    out: dict[int, dict] = {}
    sem = asyncio.Semaphore(4)
    done = 0

    async def run(batch: list[int]) -> None:
        nonlocal done
        keys: list[str] = []
        rows = []
        unplaced = False
        for n in batch:
            ks = who_in(n)
            unplaced = unplaced or not ks
            keys += [k for k in ks if k not in keys]
            rows.append(_shot_payload(n, adapted[str(n)], [sheet[k]["name"] for k in ks]))
        # A shot nobody is placed in may still show a cast member by their old
        # look alone ("the stained lapel and plaid tie knot"); only the whole
        # sheet lets that be recognised.
        ask = {"cast": list(sheet.values()) if unplaced else [sheet[k] for k in keys], "shots": rows}
        async with sem:
            try:
                data = await adapt_mod.ask_json(_SYSTEM, json.dumps(ask, ensure_ascii=False), stats,
                                                temperature=0.1)
            except Exception as exc:  # noqa: BLE001 — the shots left as they were are still usable
                logger.warning("conform batch %s failed: %s", batch, exc)
                if failed is not None:
                    failed.extend(batch)
                data = []
        for item in data if isinstance(data, list) else []:
            try:
                n = int(item["shot"])
            except (KeyError, TypeError, ValueError):
                continue
            if n not in batch:
                continue
            # Asked for {"shot", "changes"}; Opus 4.5 answers with the fields
            # beside "shot" instead. Same content, so both shapes are read.
            given = item.get("changes") if isinstance(item.get("changes"), dict) else {
                k: v for k, v in item.items() if k != "shot"}
            change = _clean(given, adapted[str(n)])
            if change:
                out[n] = change
        done += len(batch)
        if on_progress:
            on_progress("conform", done, len(numbers))

    await asyncio.gather(*(run(b) for b in batches))
    return out


def apply(adaptation: dict, changes: dict[int, dict], *, note: dict) -> dict:
    """A new adaptation dict with the changes in, and what they replaced kept.

    ``conform.before`` holds each replaced field's value from before the FIRST
    conform that touched it, so undoing restores the adaptation as written.
    """
    out = json.loads(json.dumps(adaptation))
    shots = out.setdefault("shots", {})
    record = out.setdefault("conform", {})
    before = record.setdefault("before", {})
    for n, change in changes.items():
        row = shots.get(str(n))
        if row is None:
            continue
        kept = before.setdefault(str(n), {})
        for f, v in change.items():
            kept.setdefault(f, row.get(f))
            row[f] = v
    record.update(note)
    return out


def undo(adaptation: dict) -> dict:
    """The adaptation as it was before any conform."""
    out = json.loads(json.dumps(adaptation))
    record = out.pop("conform", None) or {}
    for n, fields in (record.get("before") or {}).items():
        row = (out.get("shots") or {}).get(n)
        if row is not None:
            row.update(fields)
    return out
