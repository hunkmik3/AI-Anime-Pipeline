"""Carry an analysed video into another world without touching its editing.

Order matters and the guide is explicit about it:

  1. ENTITIES first — every person, sect, place and technique, read once from
     dialogue, burned-in name cards and on-screen subjects.
  2. GLOSSARY once — one target name per source name, then LOCKED. Renaming
     inside each shot independently is how "Kagari Ren" becomes "Ren Kagari"
     twenty shots later.
  3. SEQUENCES — beats across the whole film, so a reaction shot knows what it
     is reacting to and an off-screen voice can be attributed.
  4. SHOTS — adapted in batches against the locked glossary. Camera, blocking,
     screen direction, choreography and timing are locked; only names, world
     terms, medium and FX change.

Source analysis is never overwritten. Adaptation writes its own fields, so the
same analysis can be re-adapted to a different style without re-watching.

Adapted shots are emitted in the SAME rich schema the automation board already
uses (title / beats / performance / avoid / sfx / edit note), because that is
what the video prompt builder consumes — a one-line description per shot is
exactly the "curt shotlist" problem the board was rebuilt to fix.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from flowboard.services import avis_text

logger = logging.getLogger(__name__)

# The owner's pipeline (2026-09-24) puts GPT-6 Astra on adaptation and planning.
# Fallback handles availability errors only; content refusals stop the request.
TEXT_MODEL = os.getenv("FLOWBOARD_ADAPT_MODEL", "gpt-6-astra")
# Asked when TEXT_MODEL gives no usable answer. What looked like per-model load
# shedding on 2026-09-24 was the gateway reporting refusals INSIDE a 200 stream
# ("temperature is not supported", "Invalid or missing API key." for
# claude-opus-5) — claude-opus-4-5 was simply the model that took the request.
FALLBACK_MODEL = os.getenv("FLOWBOARD_ADAPT_FALLBACK", "claude-opus-4-5")
# Models the gateway refused outright in this process, and why.
_REFUSED: dict[str, str] = {}
SHOT_BATCH = 6
DEEP_SHOT_BATCH = 3
# Avis caps a reply at ~4k tokens unless told otherwise, and a batch of shots
# in the rich schema runs past that — Claude then returns NOTHING, with
# finish_reason "length" (4 of 13 batches on the reference clip).
MAX_TOKENS = 16000


@dataclass
class AdaptationRules:
    visual_style: str = (
        "Modern Japanese 2D anime. Clean thin line art, 2-3 solid cel-shading tones, "
        "minimal gradients. Live-action energy becomes hand-drawn impact frames, "
        "smears, speed lines and controlled bloom."
    )
    character_names: str = "Japanese names (family name first), e.g. Kagari Ren"
    sect_names: str = "English, e.g. Heavenly Mountains Sect"
    location_names: str = "English, e.g. Summit of Light"
    technique_names: str = "English, e.g. Immortal-Slaying Sword Formation"
    dialogue_mode: str = "literal"          # literal | cinematic
    dialogue_language: str = "English"      # what the adapted lines are written in
    preserve_editing: bool = True           # exact reconstruction: no merge, no split
    story_changes: str = ""                 # the owner's rewrite of the story; overrides the source

    @classmethod
    def from_dict(cls, data: dict | None) -> "AdaptationRules":
        rules = cls()
        for k, v in (data or {}).items():
            if hasattr(rules, k) and v not in (None, ""):
                setattr(rules, k, v)
        return rules

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class TextStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Which model produced each answer that was used — a fallback is not the
    # model the caller asked for, and the record should say so.
    answered_by: dict[str, int] = field(default_factory=dict)
    # Metadata only: retain no prompt or generated text in diagnostics.
    last_response: dict[str, Any] = field(default_factory=dict)

    def add(self, c: avis_text.Completion) -> None:
        self.calls += 1
        self.prompt_tokens += c.prompt_tokens
        self.completion_tokens += c.completion_tokens
        self.last_response = {"model": c.model, "finish_reason": c.finish_reason,
                              "text_chars": len(c.text), "prompt_tokens": c.prompt_tokens,
                              "completion_tokens": c.completion_tokens}


async def ask_json(system: str, user: str, stats: TextStats, *, attempts: int = 3,
                   temperature: float = 0.2, model: str | None = None,
                   fallback: str | None = None, max_tokens: int | None = None,
                   image_parts: list[dict] | None = None, transport_attempts: int | None = None) -> Any:
    """One JSON answer. A reply with no parseable JSON is asked again once,
    with the failure named — and if it still fails, the error says what came
    back (finish reason and the reply's first characters) instead of just
    "no JSON", so a truncated reply can be told apart from a chatty one.

    ``model`` / ``fallback`` default to the adaptation pair; a stage with its
    own model (the prompt writers) passes its own. The fallback is asked when
    the primary is unavailable or does not answer at all. Content refusals stop
    the request without retrying or switching models."""
    primary = model or TEXT_MODEL
    backup = FALLBACK_MODEL if fallback is None else fallback
    content = [avis_text.text_part(user), *image_parts] if image_parts else user
    first = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    last = ""
    models = [primary] * attempts
    if backup and backup != primary:
        models += [backup] * 2
    if primary in _REFUSED and len(models) > attempts:
        # Refused once this process: straight to the fallback, no waiting.
        models = models[attempts:]
        attempts = 0
    messages = first
    empty_only = True
    skip_to = -1
    for attempt, name in enumerate(models):
        if attempt < skip_to:
            continue
        if attempt >= attempts and not empty_only:
            # The fallback is for a model that is not answering, not for one
            # that answered badly — that is a question to ask again, not elsewhere.
            break
        if attempt == attempts:
            if primary in _REFUSED:
                logger.info("adapt: %s is refused (%s); asking %s", primary, _REFUSED[primary][:80], name)
            else:
                logger.warning("adapt: %s gave no usable answer; asking %s", primary, name)
            messages = first
        if attempt and attempt != attempts:
            # An EMPTY reply is not a model that misunderstood — it is the
            # gateway shedding a generation it did not want to run, and asking
            # again in the same breath gets shed again. Measured on a 432-shot
            # story pass: four calls, four empty bodies, all inside five
            # seconds. Backing off is what makes the retry mean anything.
            await asyncio.sleep(4.0 * (attempt % attempts or 1) ** 2)
        try:
            completion = await avis_text.complete(name, messages, temperature=temperature,
                                                  max_tokens=max_tokens or MAX_TOKENS,
                                                  **({'attempts':transport_attempts} if transport_attempts is not None else {}))
        except avis_text.AvisContentRefusal:
            raise
        except avis_text.AvisModelError as exc:
            # The model itself is refused: another try is the same answer.
            last = str(exc)[:200]
            logger.warning("adapt: %s refused (%s)", name, last)
            if name == primary and len(models) > attempts:
                _REFUSED[name] = last
                skip_to = attempts
                messages = first
                continue
            raise
        except avis_text.AvisTextError as exc:
            # A 520 or 502 from the gateway's edge is the same shedding as an
            # empty body, and gets the same answer: wait, then ask again.
            last = str(exc)[:200]
            logger.warning("adapt: %s failed (%s)", name, last)
            messages = first
            continue
        stats.add(completion)
        stats.last_response["max_tokens"] = max_tokens or MAX_TOKENS
        avis_text.check_content_response(completion.text, completion.finish_reason)
        try:
            value = avis_text.extract_json(completion.text)
        except avis_text.AvisTextError:
            head = completion.text.strip().replace("\n", " ")[:160]
            last = f"finish={completion.finish_reason or '?'} len={len(completion.text)} head={head!r}"
            logger.warning("adapt: %s returned no JSON (%s)", name, last)
            if not completion.text.strip():
                # Nothing came back to argue with; re-send the question as it was.
                messages = first
                continue
            empty_only = False
            messages = first + [
                {"role": "assistant", "content": completion.text},
                {"role": "user", "content": "That reply had no valid JSON. Return ONLY the JSON value, nothing else."},
            ]
            continue
        stats.answered_by[name] = stats.answered_by.get(name, 0) + 1
        stats.last_response["json_type"] = type(value).__name__
        if isinstance(value, dict):
            stats.last_response["top_level_keys"] = [str(k)[:60] for k in list(value)[:12]]
        return value
    raise avis_text.AvisTextError(f"{primary}: no JSON after {attempts} tries ({last})")


# ─────────────────────────────── 1. entities ───────────────────────────────

_ENTITY_SYSTEM = """You extract named entities from a reference video's shot analysis.

You get, per shot: a burned-in name card (the most reliable source — it spells \
names correctly), a burned-in subtitle (reliable spelling of speech), the dialogue \
heard during it (from speech recognition, so proper nouns are often MISHEARD), and \
short visual descriptions.

Return ONE JSON object:
{
  "characters": [{"source_name": "...", "aliases": ["other spellings or titles seen"],
                  "title": "rank or role if given, e.g. Master of Void Mountain Sect",
                  "visual": "what they look like on screen",
                  "evidence": "name card | dialogue | both"}],
  "sects": [{"source_name": "...", "aliases": [], "evidence": "..."}],
  "locations": [{"source_name": "...", "aliases": [], "evidence": "..."}],
  "techniques": [{"source_name": "...", "aliases": [], "evidence": "..."}]
}

Rules:
- Prefer the name-card spelling over the dialogue spelling. When dialogue has a \
near-miss of a name card ("kiếm cận tu tiên" vs a card "Kiếm Trận Tru Tiên"), they \
are ONE entity: use the card spelling and list the misheard form under aliases.
- Only list something that is actually named. A red-robed crowd is not an entity.
- Do not invent entities to fill a category; empty lists are correct.
- A FORM OF ADDRESS is not a name. "cao nhân", "senior", "elder", "master", \
"young master", "my lord" are how people address someone whose name they do not \
know; they must never become a character. A name card or a name used as one is."""


async def extract_entities(shots: list[dict], stats: TextStats, *, single_pass: bool = False) -> dict:
    evidence = []
    for s in shots:
        a = s.get("source") or {}
        line = {
            "shot": s["shot"],
            "name_card": a.get("title_card"),
            "subtitle": a.get("subtitle"),
            "dialogue": s.get("dialogue") or None,
            "subjects": a.get("subjects"),
        }
        if line["name_card"] or line["subtitle"] or line["dialogue"]:
            evidence.append(line)
    if not evidence:
        return {"characters": [], "sects": [], "locations": [], "techniques": []}
    data = await ask_json(_ENTITY_SYSTEM, json.dumps(evidence, ensure_ascii=False), stats,
                          **({'attempts':1,'fallback':'','transport_attempts':1} if single_pass else {}))
    return data if isinstance(data, dict) else {}


# ─────────────────────────────── 2. glossary ───────────────────────────────

_GLOSSARY_SYSTEM = """You build the ONE naming table for adapting a film into a new world.

Return ONE JSON object mapping every source name AND every alias to its target:
{"characters": {"<source or alias>": "<target>"}, "sects": {...},
 "locations": {...}, "techniques": {...}}

Rules:
- Every alias of an entity maps to the SAME target as its canonical name.
- Targets must be unique: two different people never share a name.
- Keep titles as titles: translate a rank into the target language alongside \
the name, e.g. "Takeda Gensai — Leader of the Immortal Alliance".
- Names should feel native to the target world, not transliterated."""


async def build_glossary(entities: dict, rules: AdaptationRules, stats: TextStats) -> dict:
    ask = {
        "naming_rules": {
            "characters": rules.character_names,
            "sects": rules.sect_names,
            "locations": rules.location_names,
            "techniques": rules.technique_names,
        },
        **({"story_changes": rules.story_changes} if rules.story_changes else {}),
        "entities": entities,
    }
    data = await ask_json(_GLOSSARY_SYSTEM, json.dumps(ask, ensure_ascii=False), stats)
    table = data if isinstance(data, dict) else {}
    return {k: dict(table.get(k) or {}) for k in ("characters", "sects", "locations", "techniques")}


def flat_glossary(glossary: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for table in glossary.values():
        out.update(table or {})
    return out


# ─────────────────────────────── 3. sequences ──────────────────────────────

_SEQUENCE_SYSTEM = """You read a whole film's shot analysis and divide it into dramatic sequences.

Return ONE JSON array:
[{"first_shot": <int>, "last_shot": <int>, "title": "short title",
  "goal": "what this sequence does in the story",
  "conflict": "who is against whom", "beats": ["..."]}]

Rules:
- Sequences are contiguous and together cover every shot, in order.
- Break on dramatic turns, not on shot counts.
- Describe events, do not rename anyone — use the names as given."""


async def build_sequences(shots: list[dict], stats: TextStats, *, single_pass: bool = False) -> list[dict]:
    compact = [
        {
            "shot": s["shot"],
            "size": (s.get("source") or {}).get("shot_size"),
            "action": (s.get("source") or {}).get("action"),
            "dialogue": s.get("dialogue") or None,
        }
        for s in shots
    ]
    data = await ask_json(_SEQUENCE_SYSTEM, json.dumps(compact, ensure_ascii=False), stats,
                          **({'attempts':1,'fallback':'','transport_attempts':1} if single_pass else {}))
    seqs = data if isinstance(data, list) else []
    return tile_sequences([q for q in seqs if isinstance(q, dict)], len(shots))


def tile_sequences(seqs: list[dict], shot_count: int) -> list[dict]:
    """Force the model's grouping to cover shots 1..N exactly once, in order.

    The model picks where the dramatic turns are; it does not get to leave a
    shot out of every sequence or put one in two. Boundaries are kept where they
    are sane and repaired where they are not: each sequence starts right after
    the previous one ends, and the last one runs to the final shot.
    """
    if shot_count <= 0:
        return []
    cleaned: list[dict] = []
    for q in seqs:
        try:
            a = int(q.get("first_shot"))
        except (TypeError, ValueError):
            continue
        cleaned.append({**q, "first_shot": a})
    cleaned.sort(key=lambda q: q["first_shot"])

    out: list[dict] = []
    nxt = 1
    for i, q in enumerate(cleaned):
        start = nxt
        following = cleaned[i + 1]["first_shot"] if i + 1 < len(cleaned) else shot_count + 1
        end = min(max(following - 1, start), shot_count)
        if start > shot_count:
            break
        out.append({**q, "first_shot": start, "last_shot": end})
        nxt = end + 1
    if not out:
        return [{"first_shot": 1, "last_shot": shot_count, "title": "Full video", "goal": "", "conflict": "", "beats": []}]
    out[-1]["last_shot"] = shot_count
    return out


# ─────────────────────────────── 4. shots ──────────────────────────────────

def cast_sheet_from_bible(cast: dict) -> list[dict]:
    """Who is who, from the stored bible — the only source that works for a
    video that names nobody on screen."""
    out = []
    for c in (cast or {}).get("characters") or []:
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "title": c.get("role") or None,
            "looks_like": c.get("looks_like") or " / ".join(
                str(st.get("look") or "") for st in c.get("states") or []
            ),
        })
    return out


def cast_sheet(entities: dict, glossary: dict) -> list[dict]:
    """Who is who, by LOOK, in target names.

    The vision pass describes people as "bald man, black-red robe" because a
    name only appears on the shot that carries the name card. Without this
    sheet the adaptation calls the antagonist "the bald man" in forty shots.
    """
    table = (glossary or {}).get("characters") or {}
    sheet = []
    for c in (entities or {}).get("characters") or []:
        target = table.get(c.get("source_name")) or ""
        name, _, title = str(target).partition(" — ")
        if name:
            sheet.append({"name": name.strip(), "title": title.strip() or None, "looks_like": c.get("visual")})
    return sheet


# Added to the shot brief when the cast is school-age. An X-ray drama's source
# shows clothes turning see-through on high-school students; written down as the
# source shows it, that beat is refused at every later step, and the clip with it.
# The owner's own version of the scene makes the power see through OBJECTS.
_MINORS_RULE = """

CONTENT — THE CAST IS SCHOOL-AGE. Never describe anyone undressed, in underwear, \
with clothing removed, dissolved or turned see-through, or framed sexually (no \
lingering on bodies, thighs, chests; no intimate close-ups). When a source shot \
does any of that, keep the beat's job in the story and move the effect onto an \
OBJECT — a bag, a book, a locker, a pencil case turning see-through — or onto the \
reaction. Clothing stays opaque and unchanged in every shot."""


def _story_rule(story: str) -> str:
    """The owner's rewrite of the story. The editing stays the source's; what it means does not."""
    return f"""

STORY CHANGES — the owner's rewrite of this adaptation. Where they conflict with the \
source, they win, inside dialogue too:
{story}
- A spoken line the changes rule out is rewritten to serve the new story: same speaker, \
same shot, same place in the scene, about the same length (within 20% of its words) and \
the same emotional beat. Never add, drop or move a line.
- Costumes, props, set dressing and world terms follow the new story.
- Camera and blocking stay locked. Where a locked action is ruled out, keep the movement \
and change what it does (an unzip opens a costume over clothes that stay on)."""


def _shot_system(rules: AdaptationRules, glossary: dict[str, str], cast: list[dict] | None = None,
                 minors: bool = False) -> str:
    return (_shot_system_text(rules, glossary, cast) + (_story_rule(rules.story_changes) if rules.story_changes else "")
            + (_MINORS_RULE if minors else ""))


def _shot_system_text(rules: AdaptationRules, glossary: dict[str, str], cast: list[dict] | None = None) -> str:
    return f"""You adapt live-action reference shots into a new production, shot by shot.

TARGET STYLE: {rules.visual_style}

WHO IS WHO — identify people in each shot by how they look, and call them by \
these names. A person matching a description IS that character, in every shot, \
even when no name card or dialogue names them there. Only someone matching no \
entry stays a description ("a disciple in red"):
{json.dumps(cast or [], ensure_ascii=False, indent=1)}

LOCKED — reproduce exactly from the source analysis, never change:
shot order, shot size, camera angle, camera movement, blocking, screen direction,
action choreography, reaction timing. {"Do not merge or split shots." if rules.preserve_editing else ""}

CHANGE ONLY:
- the medium, to the target style;
- names and world terms, STRICTLY by this locked glossary (source → target):
{json.dumps(glossary, ensure_ascii=False, indent=1)}
- live-action energy effects into the target style's FX vocabulary.
Any name NOT in the glossary is left exactly as it is. Never invent a new name.

DIALOGUE: mode={rules.dialogue_mode}, written in {rules.dialogue_language}.
"literal" keeps the meaning line for line; "cinematic" may tighten wording but \
never adds a line or moves one to another shot. Apply the glossary inside dialogue too.

Each spoken line is given ONCE, on the shot where it starts, as "dialogue_here". \
Rules that follow from that:
- Write a line only in the shot that was given it. NEVER repeat a line, or any \
part of one, in another shot — a burned-in subtitle stays on screen across cuts, \
so a neighbour showing the same words is the same line, not a new one.
- A shot with "dialogue_continues_from" is the middle of a line that started \
earlier: its "dialogue" is [] and the ongoing speech shows in "performance" instead.
- Write the whole line as one sentence. Do not chop it into fragments with dashes.
- "dialogue_heard" is rough speech recognition, for timing and emphasis only; \
"dialogue_here" is the accurate text. Where they disagree, the given line wins.
- Attribute the speaker: the person visibly speaking, or — when the speaker is \
off screen — the character the surrounding shots establish as talking. An \
appellation is not a name: "cao nhân" / "senior" / "elder" stays a form of \
address, and is never swapped for the person's name.

Write direction a video model can perform, not a summary. The model is literal \
and will overplay anything vague.

Write it the way a DOP and a director would hand it over:
- "framing_note" places things in the frame. Not "he stands there" — which third \
of frame, what is in front of him, what the background does behind him.
- "lighting" is the shot's own light, not the location's general mood: where the \
key comes from, what colour it is, what moves or changes while the shot runs.
- "action" beats are physical and ordered; "performance" is the body, never the feeling.
- Every field must be specific enough that two people reading it would build the \
same shot.

Return ONE JSON array, one object per shot given, in order:
[{{
  "shot": <int as given>,
  "title": "SHORT CAPS BEAT NAME",
  "lens_mm": "a range, e.g. 70-100",
  "camera": "angle, movement, depth of field, lens feel — matching the source",
  "framing_note": "what sits where in frame: foreground, midground, background, and where the eye goes",
  "lighting": "key and its direction, fill, rim, practicals, colour temperature, what the light does DURING the shot",
  "action": ["1-4 short physical beats, in order"],
  "dialogue": [{{"who": "SPEAKER NAME IN CAPS — the name only, never the title", "line": "..."}}],
  "performance": ["2-5 observable micro-actions, in order — the body, not the feeling"],
  "avoid": ["1-3 things the video model must NOT do in this shot"],
  "sfx": ["discrete layered sounds"],
  "vfx": "effects that HAPPEN in the shot (energy, impacts, dust), in target style, or null — not the art style itself",
  "edit_note": "timing note if the beat depends on it, else null",
  "adapted_description": "one line: size + angle + what happens, in target names"
}}]"""


async def adapt_shots(
    shots: list[dict],
    sequences: list[dict],
    glossary: dict,
    rules: AdaptationRules,
    stats: TextStats,
    *,
    on_progress=None,
    only: set[int] | None = None,
    entities: dict | None = None,
    cast: dict | None = None,
    deep: bool = False,
) -> dict[int, dict]:
    """Adapt ``shots`` (or just the numbers in ``only``) in batches.

    A failed batch is logged and skipped rather than raised: the shots that did
    come back are kept, and the caller re-runs with ``only`` set to the gaps."""
    flat = flat_glossary(glossary)
    sheet = cast_sheet_from_bible(cast or {}) or cast_sheet(entities or {}, glossary)
    from flowboard.services import automation      # local: automation is heavy, and only this needs it
    minors = automation.school_age(list((cast or {}).get("characters") or []), None)
    system = _shot_system(rules, flat, sheet, minors=minors)
    if rules.dialogue_mode == 'verbatim':
        system += ("\nVERBATIM SOURCE DIALOGUE OVERRIDE: Preserve every dialogue_here string "
                   "exactly, including punctuation and original language. No translation, tightening "
                   "or naming substitutions within spoken lines. Keep line order and owning shot.")
    if only is not None:
        # Neighbours stay in the list for context of numbering only; the model
        # is sent just the shots being filled.
        shots = [s for s in shots if s["shot"] in only]

    def seq_for(n: int) -> dict:
        return next((q for q in sequences if q["first_shot"] <= n <= q["last_shot"]), {})

    size = DEEP_SHOT_BATCH if deep else SHOT_BATCH
    batches = [shots[i : i + size] for i in range(0, len(shots), size)]
    results: dict[int, dict] = {}
    sem = asyncio.Semaphore(max(1, min(32, int(os.getenv('FLOWBOARD_ADAPT_CONCURRENCY', '8')))))
    done = 0

    async def run(batch: list[dict]) -> None:
        nonlocal done
        payload = [
            {
                "shot": s["shot"],
                "duration_s": round(s["end"] - s["start"], 2),
                "sequence": {k: seq_for(s["shot"]).get(k) for k in ("title", "goal")},
                "source": s.get("source"),
                "dialogue_here": s.get("dialogue_lines") or [],
                "dialogue_continues_from": s.get("dialogue_continues"),
                "dialogue_heard": s.get("dialogue_heard") or None,
            }
            for s in batch
        ]
        async with sem:
            try:
                data = await ask_json(system, json.dumps(payload, ensure_ascii=False), stats)
            except Exception as exc:  # noqa: BLE001 — keep the rest of the film
                logger.warning("adapt batch %s failed: %s", [s["shot"] for s in batch], exc)
                data = []
        for item in data if isinstance(data, list) else []:
            try:
                results[int(item["shot"])] = item
            except (KeyError, TypeError, ValueError):
                continue
        done += len(batch)
        if on_progress:
            on_progress(done, len(shots))

    await asyncio.gather(*(run(b) for b in batches))
    return results
