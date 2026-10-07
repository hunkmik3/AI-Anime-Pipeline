"""Turn an analysed + adapted reference video into an /automation board.

The shotlist keeps every editorial shot. The board cannot: Seedance makes one
clip per call, will not go under 4s, and gets unstable past ~20s, while a
fight edit is made of 0.3-2s shots. So shots are GROUPED into clips — never
merged, never re-timed. Each clip is a board "sequence" whose shots are the
reference shots with their measured durations, and the video prompt cuts
between them at those timestamps.

Clips never straddle a dramatic sequence boundary when they can avoid it,
because a clip that starts in one beat and ends in the next is the hardest
kind for a video model to hold together.

Cast and places are the one model call here: the analysis knows who is on
screen as visual descriptions and name cards, the board needs production-bible
entries (identity anchor, wardrobe, lighting, lock) in the TARGET world.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from typing import Any, Optional

from flowboard.services import automation, avis_text
from flowboard.services.video_analyzer import adapt as adapt_mod
from flowboard.services.video_analyzer.probe import timecode
from flowboard.services.video_analyzer.production import attach_inventory

logger = logging.getLogger(__name__)

MIN_CLIP_S = 4.0      # Seedance's floor
MAX_CLIP_S = 20.0     # default ceiling for one clip
HARD_MAX_S = 30.0     # Seedance 2.5's own limit — a short tail may push a clip this far
# The shortest shot Seedance will actually cut. Measured 2026-09-21 by matching
# the real cuts of three generated clips back to the shots they were written
# from: 48 shots written, 35 delivered, and NOTHING invented — the model does
# not hallucinate, it silently drops what it cannot fit. Delivered shots floor
# at 0.9-1.1s each whatever is asked, and the dropped ones ran a median 0.40s
# against 0.70s for the survivors. Writing a 0.2s insert therefore does not buy
# a 0.2s insert; it buys a coin toss over which beat disappears. Merging them
# here means WE choose what the cut loses, not the model.
SHOT_FLOOR_S = 1.0

# Longer clips mean fewer seams: every clip boundary is a place where the cast
# and the light can drift, and 2.5 will take 30 seconds in one call. The board
# passes what the user picked; these are only the defaults.

_FRAMING = {
    "EWS": "EWS", "WS": "WIDE", "MWS": "MWS", "MS": "MS", "MCU": "MCU",
    "CU": "CU", "ECU": "ECU", "INSERT": "INSERT", "OTS": "OTS", "POV": "POV",
}


def plan_clips(
    shots: list[dict],
    sequences: list[dict],
    *,
    max_clip_s: float = MAX_CLIP_S,
    min_clip_s: float = MIN_CLIP_S,
) -> list[dict]:
    """Group shots into clips of ``min_clip_s``..``max_clip_s`` inside each sequence.

    Returns ``[{"sequence": i, "shots": [shot numbers]}]`` in running order.
    Every shot lands in exactly one clip.
    """
    max_clip_s = max(min_clip_s, min(float(max_clip_s), HARD_MAX_S))
    hard_max = min(HARD_MAX_S, max_clip_s * 1.35)
    by_n = {s["shot"]: s for s in shots}
    dur = lambda n: by_n[n]["end"] - by_n[n]["start"]  # noqa: E731
    clips: list[dict] = []

    for qi, q in enumerate(sequences):
        numbers = [n for n in range(q["first_shot"], q["last_shot"] + 1) if n in by_n]
        mine: list[list[int]] = []
        cur: list[int] = []
        cur_s = 0.0
        for n in numbers:
            if cur and cur_s + dur(n) > max_clip_s and cur_s >= min_clip_s:
                mine.append(cur)
                cur, cur_s = [], 0.0
            cur.append(n)
            cur_s += dur(n)
        if cur:
            tail_s = sum(dur(n) for n in cur)
            last_s = sum(dur(n) for n in mine[-1]) if mine else 0.0
            if mine and tail_s < min_clip_s and last_s + tail_s <= hard_max:
                mine[-1] += cur
            else:
                mine.append(cur)
        clips += [{"sequence": qi, "shots": c} for c in mine]

    # A whole sequence shorter than the floor joins its neighbour — crossing a
    # beat is better than asking Seedance for a 2s clip it will pad with invention.
    merged: list[dict] = []
    for c in clips:
        c_s = sum(dur(n) for n in c["shots"])
        if merged and c_s < min_clip_s and sum(dur(n) for n in merged[-1]["shots"]) + c_s <= hard_max:
            merged[-1]["shots"] += c["shots"]
        else:
            merged.append(c)
    return merged


def plan_dialogue_clips(shots, sequences, dialogue_track, max_clip_s, *, character_states=None,
                        state_specific_references=False):
    """Never cut a spoken sentence between independent generation requests.

    Keep every source shot. Select a shorter safe boundary, or a bounded longer
    clip when necessary. Each character has one wardrobe reference per clip, so
    a wardrobe change also requires a safe cut. A take/sentence over the provider
    limit or crossing that wardrobe cut needs attention, rather than losing speech.
    """
    result = []
    i = 0
    while i < len(shots):
        start = shots[i]['start']
        safe = []
        clip_states = {}
        wardrobe_boundary = None
        for j in range(i, len(shots)):
            states = (character_states or {}).get(shots[j]['shot'], {})
            if any(aid in clip_states and clip_states[aid] != state for aid, state in states.items()) and not (
                    state_specific_references and not safe):
                wardrobe_boundary = shots[j]['shot']
                break
            clip_states.update(states)
            end = shots[j]['end']
            if end-start > HARD_MAX_S + .001: break
            crosses = any(d['start'] < end-.001 and d['end'] > end+.001 for d in dialogue_track)
            if not crosses: safe.append(j)
            if end-start >= max_clip_s and safe: break
        preferred = [j for j in safe if shots[j]['end']-start <= max_clip_s+.001]
        if not safe:
            if wardrobe_boundary is not None:
                raise ValueError(f"Source shot {wardrobe_boundary}: wardrobe change crosses a spoken sentence; "
                                 "no dialogue-safe boundary before the change.")
            raise ValueError(f"Source shot {shots[i]['shot']}: no dialogue-safe boundary within {HARD_MAX_S}s.")
        last = preferred[-1] if preferred else safe[0]
        qi = next((k for k,q in enumerate(sequences) if q['first_shot'] <= shots[i]['shot'] <= q['last_shot']), 0)
        result.append({'sequence':qi, 'shots':[s['shot'] for s in shots[i:last+1]]})
        i = last+1
    return result


_BIBLE_SYSTEM = """You write the production bible for re-making a reference video in a new world.

You get the target style, the naming rules, the LOCKED naming glossary, the \
named people the analysis found (often NONE — many videos name nobody), the \
people seen shot by shot as visual descriptions, and the places it shows.

Identify people by LOOK, not by name. The same description recurring across \
shots ("man in dark armour, blood on his mouth") is ONE character, and is a \
character whether or not anyone says his name.

Return ONE JSON object and nothing else:
{
  "title": "short title for the re-make, in the target world",
  "logline": "one sentence, max 40 words, target names only",
  "characters": [{
    "key": "lowercase ascii slug",
    "source_asset_id": "exact character id from source_inventory, when provided",
    "name": "TARGET name — from the glossary when the reference named this person, otherwise one you invent by the naming rules. The NAME only, no title after it",
    "source_name": "the reference name, or null when the reference never names them",
    "named_by": "glossary | invented",
    "role": "e.g. lead, antagonist, elder, witness",
    "summary": "1-2 sentences: who they are and what they want",
    "identity_anchor": "one or two physical marks that survive every state",
    "looks_like": "the reference description this person is recognised by",
    "states": [{"key": "slug", "label": "e.g. battle-worn",
                "look": "face, build, hair — in the TARGET style",
                "wardrobe": "garments, colours, materials",
                "posture": "how they hold themselves"}]
  }],
  "environments": [{
    "key": "lowercase ascii slug",
    "source_asset_id": "exact environment id from source_inventory, when provided",
    "name": "short name, target world",
    "summary": "what is in the place and its central element",
    "lighting": "light sources and how they mix",
    "mood": "stated as a contrast",
    "lock": "what must stay identical when revisited",
    "settings": ["the reference setting lines this place covers"]
  }]
}

Rules:
- Cover EVERY character id in source_inventory, including supporting and unnamed \
people. Reuse its source_asset_id exactly. Recurring background groups and props \
are preserved separately by the host; never merge them into a protagonist.
- Keep their look, build, hair and costume faithful to the reference, translated \
into the target style — do not redesign them.
- A person the reference names keeps the glossary's target name, never an \
invented one. A person it never names gets a name you invent by the naming \
rules, and "named_by": "invented".
- "looks_like" is what a later pass matches shots against: build, hair, face, \
costume, marks — in the words the analysis itself used.
- One state each, unless the reference visibly changes them (wounds, torn \
clothes, a costume change) — then one per look, max three.
- Environments: one per place a viewer would recognise as a DIFFERENT location, \
up to twelve. A cliff top overlooking a canyon is not the canyon floor; a \
corridor is not the room it leads to; the same street by night is not the same \
street by day. Only the ground, sky, rubble and rock faces of ONE place, seen \
from different angles, are the same environment.
- Every setting line you are given must belong to exactly one environment — \
list the ones it covers under "settings". A line you cannot place goes to the \
closest place rather than being dropped.
- Each environment is a plate someone will generate, so describe it as a place \
to be photographed: what is in it, its light, its materials."""

_WHERE_SYSTEM = """You say who is on screen and where, for shots of a reference video.

You get the cast (target name + how that person looks in the reference), the \
places (key + which reference settings they cover), and a batch of shots with \
their setting, the people visible and any name card.

Return ONE JSON array and nothing else, one entry per shot given:
[{"shot": <int>, "character_keys": ["..."], "environment_key": "..."}]

Rules:
- Match people by LOOK. A description matching a cast entry IS that character.
- These keys cover individual characters and places only. Background groups and \
props have a separate verified inventory which the host joins after this pass; \
do not interpret an absent character key as an empty background.
- Use only the keys given. An insert of a hand or a foot still names whose it is \
when the surrounding shots make it obvious."""

SHOT_MAP_BATCH = 30


def _split_prompts() -> tuple[str, str]:
    """The bible prompt, as two: cast, then places.

    One call for both produced an EMPTY reply from the gateway on a 215-shot
    film — the answer (eight characters and nine locations, each a paragraph)
    simply ran past what it would stream back. Two smaller answers arrive.
    """
    head, _, rest = _BIBLE_SYSTEM.partition("Return ONE JSON object and nothing else:")
    body, _, rules = rest.partition("Rules:")
    chars_block = body[: body.index('  "environments"')].rstrip().rstrip(",") + "\n}"
    places_block = (
        '{\n  "environments": '
        + body[body.index('  "environments": ') + len('  "environments": ') : body.rindex("}")].rstrip().rstrip(",")
        + "\n}"
    )
    common = head + "Return ONE JSON object and nothing else:\n"
    rules = "Rules:" + rules
    cast_rules = "\n".join(l for l in rules.splitlines() if "nvironment" not in l)
    place_rules = "\n".join(
        l for l in rules.splitlines()
        if "nvironment" in l or l.strip() in ("Rules:",) or l.startswith("Rules:")
    )
    return common + chars_block + "\n\n" + cast_rules, common + places_block + "\n\n" + place_rules


async def build_bible(analysis: dict, adaptation: dict, stats: adapt_mod.TextStats) -> dict:
    rules = adaptation.get("rules") or {}
    settings: list[str] = []
    for s in analysis["shots"]:
        setting = (s.get("source") or {}).get("setting")
        if setting and setting not in settings:
            settings.append(setting)
    # Who is on screen, shot by shot. Without this a video that names nobody —
    # most of them — produces an empty cast, because the entity pass only ever
    # sees names.
    people = [
        {"shot": s["shot"], "subjects": (s.get("source") or {}).get("subjects")}
        for s in analysis["shots"]
        if (s.get("source") or {}).get("subjects")
    ]
    ask = {
        "target_style": rules.get("visual_style"),
        "naming_rules": {
            "characters": rules.get("character_names"),
            "locations": rules.get("location_names"),
        },
        "glossary": adaptation.get("glossary") or {},
        "named_people": (analysis.get("entities") or {}).get("characters") or [],
        "people_seen": people,
        "settings_seen": settings,
        "source_inventory": (analysis.get("scene_inventory") or {}).get("assets") or [],
    }
    cast_prompt, place_prompt = _split_prompts()
    # Two calls, run together: the cast question does not need the settings list
    # and the places question does not need every subject ever seen.
    cast_ask = {k: v for k, v in ask.items() if k != "settings_seen"}
    place_ask = {k: v for k, v in ask.items() if k != "people_seen"}
    people, places = await asyncio.gather(
        adapt_mod.ask_json(cast_prompt, json.dumps(cast_ask, ensure_ascii=False), stats, temperature=0.3),
        adapt_mod.ask_json(place_prompt, json.dumps(place_ask, ensure_ascii=False), stats, temperature=0.3),
        return_exceptions=True,
    )
    def unwrap(value, key: str) -> dict:
        """Take the answer whether it came wrapped or bare.

        Asked for one half of the bible, the model often replies with just the
        list — which is a correct answer to the question, and was being thrown
        away as a failure.
        """
        if isinstance(value, list):
            return {key: value}
        return value if isinstance(value, dict) else {}

    people_data, places_data = unwrap(people, "characters"), unwrap(places, "environments")
    if not people_data:
        logger.warning("board: cast half of the bible failed: %s", people)
    if not places_data:
        logger.warning("board: places half of the bible failed: %s", places)
    out: dict = dict(people_data)
    out["environments"] = places_data.get("environments") or []
    out.setdefault("title", places_data.get("title") or "")
    out.setdefault("logline", places_data.get("logline") or "")
    if not out.get("characters") and not out.get("environments"):
        raise RuntimeError("bible: both halves failed")
    return out


async def map_shots(
    analysis: dict, characters: list[dict], environments: list[dict], stats: adapt_mod.TextStats
) -> dict[int, dict]:
    """Who and where, per shot. Batched — 101 shots in one reply is where the
    single combined call used to run out of room and return nothing."""
    cast = [
        {"key": c["key"], "name": c.get("name"), "looks_like": c.get("looks_like") or
         " / ".join(st.get("look", "") for st in c.get("states") or [])}
        for c in characters
    ]
    places = [{"key": e["key"], "name": e.get("name"), "settings": e.get("settings")} for e in environments]
    rows = [
        {
            "shot": s["shot"],
            "setting": (s.get("source") or {}).get("setting"),
            "subjects": (s.get("source") or {}).get("subjects"),
            "name_card": (s.get("source") or {}).get("title_card"),
        }
        for s in analysis["shots"]
    ]
    batches = [rows[i : i + SHOT_MAP_BATCH] for i in range(0, len(rows), SHOT_MAP_BATCH)]
    out: dict[int, dict] = {}
    sem = asyncio.Semaphore(4)

    async def run(batch: list[dict]) -> None:
        async with sem:
            try:
                data = await adapt_mod.ask_json(
                    _WHERE_SYSTEM,
                    json.dumps({"cast": cast, "places": places, "shots": batch}, ensure_ascii=False),
                    stats,
                )
            except Exception as exc:  # noqa: BLE001 — an unmapped shot still lands on the board
                logger.warning("board: shot mapping batch %s failed: %s", batch[0]["shot"], exc)
                return
        for item in data if isinstance(data, list) else []:
            try:
                out[int(item["shot"])] = item
            except (KeyError, TypeError, ValueError):
                continue

    await asyncio.gather(*(run(b) for b in batches))
    return out


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s or fallback


def _lines(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(v).strip() for v in value or [] if str(v).strip()]


def board_shot(n_in_clip: int, shot: dict, adapted: dict, cast: dict[int, dict]) -> dict:
    src = shot.get("source") or {}
    size = str(src.get("shot_size") or "").upper()
    camera = adapted.get("camera") or ", ".join(
        x for x in [src.get("camera_angle"), src.get("camera_movement")] if x
    )
    avoid = _lines(adapted.get("avoid"))
    vfx = adapted.get("vfx")
    action = _lines(adapted.get("action")) or _lines(src.get("action"))
    if vfx:
        action.append(f"VFX: {vfx}")
    return {
        "n": n_in_clip,
        "title": adapted.get("title") or "",
        "duration_s": round(shot["end"] - shot["start"], 6),
        "framing": _FRAMING.get(str(adapted.get('framing') or size).upper(), adapted.get('framing') or size or "MS"),
        "lens_mm": str(adapted.get("lens_mm") or ""),
        "camera": camera,
        "framing_note": adapted.get("framing_note") or "",
        "lighting": adapted.get("lighting") or "",
        "action": action,
        "dialogue": [d for d in adapted.get("dialogue") or [] if isinstance(d, dict) and d.get("line")],
        "performance": _lines(adapted.get("performance")),
        "avoid": avoid,
        "sfx": _lines(adapted.get("sfx")),
        "edit_note": adapted.get("edit_note") or "",
        # What the reference's audio says inside this shot's own span, and
        # which shot its line began in when it runs across the cut — what
        # _carry_lines splits a line by.
        "heard": shot.get("dialogue_heard") or "",
        "continues_from": shot.get("dialogue_continues"),
        "character_keys": list((cast.get(shot["shot"]) or {}).get("character_keys") or []),
        "character_states": dict((cast.get(shot['shot']) or {}).get('character_states') or {}),
        **{key: (cast.get(shot["shot"]) or {}).get(key, [] if key != "environment_key" and key != "scene_id" else "")
           for key in ("asset_keys", "asset_presence", "scene_present_asset_ids", "scene_asset_ids", "source_evidence",
                       "source_appearances", "environment_key", "scene_id")},
        # Where this shot sits in the reference, for whoever checks the re-make against it.
        "source_shot": shot["shot"],
        "source_start": shot["start"], "source_end": shot["end"],
        "source_tc": f"{timecode(shot['start'])}–{timecode(shot['end'])}",
    }


def _merge_unshootable(shots: list[dict], floor: float = SHOT_FLOOR_S) -> list[dict]:
    """Fold shots the model cannot cut into the one before them.

    An insert under a second is a beat inside its neighbour, not a shot of its
    own — that is how an editor would take the note, and it keeps the written
    list and the delivered cut the same length. The absorbed beat's action,
    lines and effects all survive; only its separate cut does not.

    The first shot has nothing before it, so a short opener absorbs forward.
    """
    if not shots:
        return []

    def absorb(into: dict, extra: dict) -> dict:
        merged = dict(into)
        merged["duration_s"] = round(float(into.get("duration_s") or 0)
                                     + float(extra.get("duration_s") or 0), 2)
        for field in ("action", "performance", "avoid", "sfx"):
            merged[field] = list(into.get(field) or []) + list(extra.get(field) or [])
        merged["dialogue"] = list(into.get("dialogue") or []) + list(extra.get("dialogue") or [])
        merged["heard"] = " ".join(x for x in (into.get("heard"), extra.get("heard")) if x)
        keys = list(into.get("character_keys") or [])
        keys += [k for k in extra.get("character_keys") or [] if k not in keys]
        merged["character_keys"] = keys
        for field in ("asset_keys", "scene_present_asset_ids", "scene_asset_ids", "source_evidence"):
            merged[field] = list(dict.fromkeys(list(into.get(field) or []) + list(extra.get(field) or [])))
        # Keep the order of source beats: the same prop may change hands inside
        # this merged shot. Unioning by asset id would erase that transition.
        for field in ("asset_presence", "source_appearances"):
            merged[field] = list(into.get(field) or []) + list(extra.get(field) or [])
        merged["environment_keys"] = list(dict.fromkeys(
            [k for row in (into, extra) for k in (row.get("environment_keys") or [row.get("environment_key")]) if k]))
        # Both titles, so the beat that was folded in is still named on the board.
        if extra.get("title") and extra["title"] not in (into.get("title") or ""):
            merged["title"] = " / ".join(x for x in (into.get("title"), extra["title"]) if x)
        merged["source_shots"] = (list(into.get("source_shots") or [into.get("source_shot")])
                                  + list(extra.get("source_shots") or [extra.get("source_shot")]))
        merged["source_tc"] = (f"{(into.get('source_tc') or '').split('–')[0]}–"
                               f"{(extra.get('source_tc') or '–').split('–')[-1]}")
        return merged

    # Close each shot as soon as it reaches the floor. Folding every short shot
    # into a single growing neighbour instead would turn a 14-shot fight into
    # two four-second holds — the beats survive, but the cutting does not, and
    # the cutting is what a fight is.
    out: list[dict] = []
    cur: Optional[dict] = None
    for shot in shots:
        cur = dict(shot) if cur is None else absorb(cur, shot)
        if float(cur.get("duration_s") or 0) >= floor:
            out.append(cur)
            cur = None
    if cur is not None:                      # a short tail joins the shot before it
        if out:
            out[-1] = absorb(out[-1], cur)
        else:
            out.append(cur)
    for i, shot in enumerate(out, start=1):
        shot["n"] = i
        shot.setdefault("source_shots", [shot.get("source_shot")])
    return out


def _words(text: str) -> list[str]:
    return [w for w in (re.sub(r"[^\w']", "", t.lower().replace("’", "'")) for t in str(text or "").split()) if w]


def _split_at_heard(line: str, heard_tail: str, heard_head: str = "") -> tuple[str, str]:
    """Cut a line where the reference's cut falls in it.

    The tail is found by its opening words as heard after the cut; when the
    adaptation reworded it past recognition, the split falls at the same share
    of the line the audio spent on each side, moved to the nearest comma or
    full stop so neither half ends mid-phrase.
    """
    tokens = str(line or "").split()
    norm = [(_words(t) or [""])[0] for t in tokens]
    tail = _words(heard_tail)
    if len(tokens) < 2 or not tail:
        return line, ""
    for width in (3, 2):
        probe = tail[:width]
        if len(probe) < width:
            continue
        hits = [k for k in range(1, len(tokens)) if norm[k:k + width] == probe]
        if hits:
            k = hits[-1]
            return " ".join(tokens[:k]), " ".join(tokens[k:])
    head = _words(heard_head)
    share = len(tail) / max(1, len(tail) + len(head)) if head else 0.5
    k = max(1, min(len(tokens) - 1, round(len(tokens) * (1 - share))))
    stops = [i + 1 for i, t in enumerate(tokens[:-1]) if t[-1:] in ",.;:!?—…"]
    near = min(stops, key=lambda i: abs(i - k)) if stops else k
    k = near if abs(near - k) <= 2 else k
    return " ".join(tokens[:k]), " ".join(tokens[k:])


def _carry_lines(shots: list[dict]) -> list[dict]:
    """Split a line the reference speaks across a cut, at that cut.

    The adaptation writes a line once, on the shot where it starts, and the
    shot it runs on into says only that the speech continues. Written that way
    the next shot reads "There is NO dialogue", so the whole line has to fit the
    first — and "No, listen, in ten minutes, if you don't see the nurse, you
    will die." does not fit 2.3 seconds. The reference said its second half over
    the listener's reaction; so does the re-make.
    """
    where: dict[Any, int] = {}
    for i, shot in enumerate(shots):
        for src in shot.get("source_shots") or [shot.get("source_shot")]:
            where[src] = i
    holder: dict[Any, int] = {}          # where the unsaid rest of a line now sits
    for i, shot in enumerate(shots):
        origin = shot.get("continues_from")
        if origin is None or shot.get("dialogue"):
            continue
        j = holder.get(origin, where.get(origin))
        if j is None or j >= i:          # began in the clip before, or folded into this shot
            continue
        said = shots[j].get("dialogue") or []
        if not said:
            continue
        head, tail = _split_at_heard(said[-1]["line"], shot.get("heard") or "", shots[j].get("heard") or "")
        if not head or not tail:
            continue
        said[-1] = {**said[-1], "line": head}
        shot["dialogue"] = [{**said[-1], "line": tail, "cont": True}]
        holder[origin] = i
    return shots


def _renamer(characters: list[dict]):
    """Source names to cast names, for text written before the cast was named.

    The clip titles and goals come from the reading of the reference, which
    knows the actors' characters ("Michael sees the spider inside Ava"); the
    board's cast is Theo and Sienna.
    """
    swap: dict[str, str] = {}
    titles = {"dr", "mr", "mrs", "ms", "miss", "sir", "prof"}
    for c in characters:
        old, new = str(c.get("source_name") or "").strip(), str(c.get("name") or "").strip()
        if not old or not new or old == new:
            continue
        # Like for like: a first name for a first name, a title keeps its
        # title ("Dr. Skylar" → "Dr. Pryce"), a full name for a full name.
        title = [t for t in old.split() if t.rstrip(".").lower() in titles]
        given = [t for t in old.split() if t.rstrip(".").lower() not in titles]
        if title:
            swap[old] = f"{' '.join(title)} {new.split()[-1]}"
        elif len(given) == 1:
            swap[old] = new.split()[0]
        else:
            swap[old] = new
            swap.setdefault(given[0], new.split()[0])
    if not swap:
        return lambda text: text
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(swap, key=len, reverse=True))) + r")\b")
    return lambda text: pattern.sub(lambda m: swap[m.group(1)], str(text or ""))


async def build_cast(analysis: dict, adaptation: dict) -> dict:
    """Who is in this film, where it plays, and which shots each covers.

    Stored on the video, not rebuilt per board: the sheets generated from it
    cost money, and a re-adaptation must not throw them away.
    """
    stats = adapt_mod.TextStats()
    bible = await build_bible(analysis, adaptation, stats)
    characters = [c for c in bible.get("characters") or [] if isinstance(c, dict)]
    environments = [e for e in bible.get("environments") or [] if isinstance(e, dict)]
    _normalise(characters, environments)
    mapped = await map_shots(analysis, characters, environments, stats)

    char_keys = {c["key"] for c in characters}
    env_keys = {e["key"] for e in environments}
    per_shot = {
        int(n): {
            "character_keys": [k for k in item.get("character_keys") or [] if k in char_keys],
            "environment_key": item.get("environment_key") if item.get("environment_key") in env_keys else "",
        }
        for n, item in mapped.items()
    }

    # Which shots each entry actually appears in — the reviewer's way back to
    # the video, and what the board uses to wire a clip to its materials.
    by_shot = {s["shot"]: s for s in analysis["shots"]}
    for c in characters:
        c["shots"] = sorted(n for n, v in per_shot.items() if c["key"] in v["character_keys"])
        c["frames"] = _frames_for(by_shot, c["shots"])
    for e in environments:
        e["shots"] = sorted(n for n, v in per_shot.items() if v["environment_key"] == e["key"])
        e["frames"] = _frames_for(by_shot, e["shots"])
        e.setdefault("settings", [])

    return attach_inventory(analysis, {
        "title": str(bible.get("title") or "").strip(),
        "logline": str(bible.get("logline") or "").strip(),
        "characters": characters,
        "environments": environments,
        "shots": {str(n): v for n, v in sorted(per_shot.items())},
        "usage": {"model": adapt_mod.TEXT_MODEL, "calls": stats.calls,
                  "prompt_tokens": stats.prompt_tokens, "completion_tokens": stats.completion_tokens},
    })


def _frames_for(by_shot: dict[int, dict], shots: list[int], limit: int = 6) -> list[str]:
    """A few keyframes showing this person or place, spread across its shots."""
    if not shots:
        return []
    step = max(1, len(shots) // limit)
    out: list[str] = []
    for n in shots[::step][:limit]:
        frames = (by_shot.get(n) or {}).get("frames") or []
        if frames:
            out.append(frames[len(frames) // 2])
    return out


def _normalise(characters: list[dict], environments: list[dict]) -> None:
    """Slugs and states, so every later step can address an entry by key."""
    for i, c in enumerate(characters):
        c["key"] = _slug(c.get("key") or c.get("name"), f"char-{i + 1}")
        c["states"] = [st for st in c.get("states") or [] if isinstance(st, dict)] or [
            {"key": "default", "label": "as seen", "look": "", "wardrobe": "", "posture": ""}
        ]
        for j, st in enumerate(c["states"]):
            st["key"] = _slug(st.get("key") or st.get("label"), f"state-{j + 1}")
    for i, e in enumerate(environments):
        e["key"] = _slug(e.get("key") or e.get("name"), f"env-{i + 1}")


async def build_board(analysis: dict, adaptation: dict, cast: Optional[dict] = None,
                      *, max_clip_s: float = MAX_CLIP_S, preserve_source_shots: bool = True, source_id: str = "") -> dict:
    """The /api/automation/breakdown response shape, plus each clip's shots already cut.

    ``cast`` is the stored bible when there is one — reusing it keeps the board's
    names and places identical to what the reviewer approved (and to the sheets
    already generated from them)."""
    if cast is None:
        cast = await build_cast(analysis, adaptation)
    cast = attach_inventory(analysis, cast)
    if (cast.get('usage') or {}).get('method') == 'verified_source_inventory':
        # Source attachment keeps the audit catalog complete. Do not turn its
        # retired extraction candidates back into production sheet nodes.
        unused = set(cast['usage'].get('unused_candidates') or [])
        for bucket in ('characters', 'environments', 'props', 'background_groups', 'assets'):
            cast[bucket] = [e for e in cast.get(bucket, []) if e.get('source_asset_id') not in unused]
    characters = [dict(c) for c in cast.get("characters") or []]
    environments = [dict(e) for e in cast.get("environments") or []]
    per_shot = {int(n): v for n, v in (cast.get("shots") or {}).items()}

    by_n = {s["shot"]: s for s in analysis["shots"]}
    adapted_shots = adaptation.get("shots") or {}
    sequences_src = sorted(analysis.get("sequences") or [], key=lambda q:q['first_shot'])
    # Story inference may omit a shot or overlap ranges. The measured source
    # timeline is authoritative: cover every source shot exactly once.
    covered = set()
    normalized = []
    for q in sequences_src:
        ns = [n for n in sorted(by_n) if q['first_shot'] <= n <= q['last_shot'] and n not in covered]
        if not ns: continue
        # Split discontinuities instead of letting a range pull old shots back in.
        runs=[]
        for n in ns:
            if not runs or n != runs[-1][-1]+1:runs.append([])
            runs[-1].append(n)
        for run in runs:normalized.append({**q,'first_shot':run[0],'last_shot':run[-1]})
        covered.update(ns)
    for n in sorted(set(by_n)-covered):
        normalized.append({'first_shot':n,'last_shot':n,'title':'Source shot '+str(n)})
    sequences_src = sorted(normalized,key=lambda q:q['first_shot'])
    one_pass_film = (analysis.get('source_verification') or {}).get('method') == 'one_pass_production'
    if one_pass_film:
        clips = plan_dialogue_clips(analysis['shots'], sequences_src, analysis.get('dialogue_track') or [], max_clip_s,
                                   character_states={n: row.get('character_states') or {} for n, row in per_shot.items()},
                                   state_specific_references=True)
    else:
        clips = plan_clips(analysis["shots"], sequences_src, max_clip_s=max_clip_s)

    sequences: list[dict] = []
    shots_by_seq: dict[str, list[dict]] = {}
    seq_counter: Counter[int] = Counter()
    rename = _renamer(characters)
    for ci, clip in enumerate(clips, start=1):
        q = sequences_src[clip["sequence"]] if sequences_src else {}
        seq_counter[clip["sequence"]] += 1
        numbers = clip["shots"]
        first, last = by_n[numbers[0]], by_n[numbers[-1]]
        key = f"clip-{ci:02d}"
        envs = Counter(per_shot.get(n, {}).get("environment_key") for n in numbers)
        envs.pop("", None)
        envs.pop(None, None)
        cast_in = []
        for n in numbers:
            for k in per_shot.get(n, {}).get("character_keys") or []:
                if k not in cast_in:
                    cast_in.append(k)
        board_shots = [
            board_shot(i, by_n[n], adapted_shots.get(str(n)) or {}, per_shot)
            for i, n in enumerate(numbers, start=1)
        ]
        if not preserve_source_shots:
            board_shots = _merge_unshootable(board_shots)
        if not one_pass_film:
            board_shots = _carry_lines(board_shots)
        else:
            for shot in board_shots:
                shot['performance'] = [
                    f"Clip audio {line['start']-first['start']:.3f}–{line['end']-first['start']:.3f}s: " +
                    ('start assigned dialogue once; continue seamlessly across following cuts.'
                     if line['first_shot'] == shot['source_shot'] else 'carry preceding dialogue only; never restart it.')
                    for line in analysis.get('dialogue_track') or []
                    if line['first_shot'] <= shot['source_shot'] <= line['last_shot']]
        for shot in board_shots:
            shot["shot_uid"] = f"{source_id or 'source'}:shot:{shot['source_shot']}"
        titles = [s["title"] for s in board_shots if s["title"]]
        sequences.append(
            {
                "key": key,
                "label": f"CLIP {ci:02d}",
                "title": rename(q.get("title")) or (titles[0] if titles else key),
                "duration_s": round(last["end"] - first["start"], 2),
                "summary": " ".join(
                    x for x in [rename(q.get("goal")), "Beats: " + "; ".join(titles) if titles else ""] if x
                ),
                "beat": rename(q.get("conflict") or q.get("goal")),
                "environment_key": envs.most_common(1)[0][0] if envs else "",
                "character_keys": cast_in,
                "asset_keys": list(dict.fromkeys(k for n in numbers for k in per_shot.get(n, {}).get("asset_keys") or [])),
                # This clip re-makes an edited stretch of a reference: it CUTS
                # between shots at their measured times instead of playing as
                # one continuous take. The prompt builder reads this.
                "editing": "cut",
                "preserve_source_shots": preserve_source_shots,
                "source_range": f"{timecode(first['start'])}–{timecode(last['end'])} · shots "
                                f"{numbers[0]:03d}–{numbers[-1]:03d}",
            }
        )
        shots_by_seq[key] = board_shots

    video = analysis["video"]
    return {
        "title": str(cast.get("title") or "Reference re-make").strip(),
        "logline": str(cast.get("logline") or "").strip(),
        "runtime_seconds": int(round(video["duration"])),
        "characters": characters,
        "environments": environments,
        # A source contract only for a video that has a source inventory. A video
        # analysed before the inventory existed has nothing for the strict path to
        # check against; handing it an "unverified" report put its board into the
        # strict path anyway, where every prompt and every Gen was refused until a
        # verification that board could not get. It keeps working as it did; the
        # source tab still offers "Đối chiếu video gốc" to bring it into the contract.
        **({"assets": cast.get("assets") or [],
            "production_assets": cast.get("production_assets") or [],
            "source_verification": cast.get("source_verification") or {"status": "unverified"}}
           if analysis.get("scene_inventory") else {}),
        "sequences": sequences,
        "shots": shots_by_seq,
        "preserve_source_shots": preserve_source_shots,
        "state_specific_references": one_pass_film,
        "source_video_id": source_id,
        "style": automation.style_from_rules((adaptation.get("rules") or {}).get("visual_style")),
        "aspect_ratio": video.get("aspect_ratio"),
        "usage": cast.get("usage") or {},
    }
