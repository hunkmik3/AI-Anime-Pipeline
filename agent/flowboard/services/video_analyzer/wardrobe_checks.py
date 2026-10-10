"""Deterministic wardrobe and identity checks on a production catalog.

A wardrobe state is an outfit or a physical condition (wet, in a towel, undressed) — not
a camera angle, a closer view of the same clothes or a crop. The catalog model splits and
flips states by view, and sometimes describes a person one way while binding a state
that says another. These checks find that from the catalog's own words; nothing here
calls a model or reads an image.

* ``unexplained_wardrobe_change`` — inside one scene a person's state changes with no
  ``wardrobe_changes`` entry naming the visible change (this is how A→B→A flips by view,
  and a wet uniform turning dry mid-scene, show up).
* ``partial_view_state`` — a new state that only describes what a crop shows, for a
  person who already has an outfit.
* ``duplicate_state`` — a new state with exactly the outfit and look of an existing one.
* ``state_contradicts_observation`` — the shot describes the person wet, in a towel,
  bare-chested or with hair down/up, the bound state does not, and another of that
  person's states does.
* ``identity_contradicts_profile`` — a shot gives a known person another hair colour or
  facial hair than their profile: two people may have been merged.

The catalog gets one repair round for these like any other finding; what survives it is
kept as a note for the reviewer rather than failing the film.
"""
from __future__ import annotations

import re

SOFT = 'wardrobe'

_CROP = re.compile(
    r"\bcropped(?:\s+\w+)?\s+(?:view|views|frame|shot)s?\b|\bout of frame\b|\bpartially visible\b"
    r"|\bonly\s+(?:the\s+|his\s+|her\s+|their\s+)?(?:[\w,-]+\s+){0,3}"
    r"(?:legs?|feet|foot|hands?|arms?|shoulders?|head|back|torso|face|knees?)\s+(?:is\s+|are\s+)?visible\b"
    r"|\bdetails?\s+(?:are\s+|is\s+)?(?:indistinct|not visible|unclear)\b", re.I)

_FEATURES = {
    'wet': re.compile(r"\b(?:wet|soaked|dripping|drenched|damp|sodden)\b", re.I),
    'towel': re.compile(r"\btowel\b", re.I),
    'bare_top': re.compile(r"\b(?:shirtless|topless|bare[- ]chested|bare (?:upper body|torso|chest)|naked|nude|undressed)\b", re.I),
    'hair_down': re.compile(r"\b(?:hair (?:is |worn |hanging )?(?:down|loose)|loose(?:,)? (?:\w+ )?hair|let (?:her|his) hair down)\b", re.I),
    'hair_up': re.compile(r"\b(?:bun|ponytail|updo|hair (?:is )?(?:tied|pulled|gathered|pinned) (?:up|back))\b", re.I),
}
_NEGATED = re.compile(r"\b(?:no|not|without|never|isn't|aren't|un)\b[^.;,]{0,25}$", re.I)

_HAIR_COLOURS = {
    'dark': ('black', 'dark', 'brown', 'brunette', 'raven', 'chestnut'),
    'blond': ('blond', 'blonde', 'golden', 'platinum', 'flaxen'),
    'red': ('red', 'ginger', 'auburn', 'copper'),
    'grey': ('grey', 'gray', 'silver', 'white'),
}
_HAIR = re.compile(r"\b(" + "|".join(w for ws in _HAIR_COLOURS.values() for w in ws) + r")(?:[- ]\w+)?(?:[- ]\w+)? hair(?:ed)?\b", re.I)
_BEARD = re.compile(r"\b(?:beard(?:ed)?|stubble|moustache|mustache|goatee)\b", re.I)
_SHAVEN = re.compile(r"\b(?:clean[- ]shaven|beardless|no (?:beard|stubble|facial hair))\b", re.I)


def _norm(text: str) -> str:
    return ' '.join(re.findall(r"[a-z0-9]+", str(text or '').lower()))


def features(text: str) -> set[str]:
    """Physical conditions a text asserts (a negated mention asserts nothing)."""
    out = set()
    for name, pattern in _FEATURES.items():
        for m in pattern.finditer(str(text or '')):
            if not _NEGATED.search(str(text)[:m.start()][-40:]):
                out.add(name)
                break
    return out


def hair_colours(text: str) -> set[str]:
    found = set()
    for m in _HAIR.finditer(str(text or '')):
        word = m.group(1).lower()
        found |= {group for group, words in _HAIR_COLOURS.items() if word in words}
    return found


def _state_text(state: dict) -> str:
    return f"{state.get('wardrobe', '')} {state.get('look', '')} {state.get('label', '')}"


def _issue(code: str, message: str, n=None) -> dict:
    return {'code': code, 'message': message, 'kind': SOFT, **({'shot': n} if n is not None else {})}


def new_state_issues(asset: dict, new_keys: set[str]) -> list[dict]:
    """Partial-view and duplicate states among a character's newly added states."""
    issues = []
    states = asset.get('states') or []
    for st in states:
        if st.get('key') not in new_keys:
            continue
        others = [o for o in states if o.get('key') != st.get('key')]
        if others and _CROP.search(f"{st.get('wardrobe', '')} {st.get('look', '')}"):
            full = [o['key'] for o in others if not _CROP.search(f"{o.get('wardrobe', '')} {o.get('look', '')}")]
            issues.append(_issue('partial_view_state',
                f"{asset['id']}/{st['key']} only describes what a crop shows. A state is a whole outfit or "
                f"condition: give the shot the outfit {asset['id']} wears in this scene"
                + (f" ({', '.join(full[:4])})" if full else '') + ", or describe the complete new outfit."))
        same = [o['key'] for o in others if _norm(o.get('wardrobe')) == _norm(st.get('wardrobe'))
                and _norm(o.get('look')) == _norm(st.get('look'))]
        if same:
            issues.append(_issue('duplicate_state',
                f"{asset['id']}/{st['key']} repeats the outfit and look of {same[0]}; reuse {same[0]}."))
    return issues


def shot_issues(rows: list[tuple[int, dict]], assets: dict[str, dict], *, check_from: int | None = None) -> list[dict]:
    """Scene continuity and observation checks over catalog rows in film order.

    ``rows`` may start with earlier, already accepted shots for context; only shots
    numbered ``check_from`` or later are reported.
    """
    issues = []
    last: dict[tuple[str, str], tuple[int, str]] = {}      # (scene, character) -> (shot, state)
    profiles = {aid: a for aid, a in assets.items() if a.get('kind') == 'character'}
    for n, row in rows:
        report = check_from is None or n >= check_from
        scene = row.get('scene_id')
        changes = {(c.get('asset_id'), c.get('to_state')): c for c in row.get('wardrobe_changes') or []
                   if isinstance(c, dict) and str(c.get('basis') or '').strip()}
        presence = {p.get('asset_id'): p for p in row.get('asset_presence') or [] if isinstance(p, dict)}
        for aid, key in (row.get('character_states') or {}).items():
            asset = profiles.get(aid)
            if not asset:
                continue
            states = {s.get('key'): s for s in asset.get('states') or []}
            before = last.get((scene, aid))
            if report and before and before[1] != key and (aid, key) not in changes:
                earlier = [k for (sc, a), (_, k) in last.items() if sc == scene and a == aid]
                issues.append({**_issue('unexplained_wardrobe_change',
                    f"In scene {scene}, {aid} changes from {before[1]} (shot {before[0]}) to {key} with no visible "
                    f"change recorded. If the frames show it (a jacket taken off, getting wet, a time cut), add "
                    f"wardrobe_changes [{{asset_id:'{aid}', from_state:'{before[1]}', to_state:'{key}', basis}}]; "
                    f"if it is the same outfit seen closer or from another side, keep {before[1]}; if the earlier "
                    f"shots were already {key} and only these frames show it, rebind them in earlier_states "
                    f"{{'{before[0]}': {{'{aid}': '{key}'}}}}.", n),
                    'asset_id': aid, 'from_state': before[1], 'to_state': key})
            last[(scene, aid)] = (n, key)
            observed = (presence.get(aid) or {}).get('state') or ''
            if not report or not observed or key not in states:
                continue
            seen = features(observed)
            bound = features(_state_text(states[key]))
            for f in sorted(seen - bound):
                if f == 'hair_down' and 'hair_up' not in bound:
                    continue        # a state silent about hair does not contradict loose hair
                if f == 'hair_up' and 'hair_down' not in bound:
                    continue
                # The closest outfit that has the observed condition: a wet uniform, not a bath.
                mine = set(_norm(states[key].get('wardrobe')).split())
                better = sorted((k for k, s in states.items() if k != key and f in features(_state_text(s))),
                                key=lambda k: -len(mine & set(_norm(states[k].get('wardrobe')).split())))
                if better:
                    issues.append(_issue('state_contradicts_observation',
                        f"Shot {n} describes {aid} as {f.replace('_', ' ')} (\"{observed[:90]}\") but binds {key}, "
                        f"which is not; {better[0]} is. Bind the state that matches the frames, or correct the description.", n))
                    break
            profile_hair = hair_colours(f"{asset.get('description', '')} {asset.get('name', '')}")
            shot_hair = hair_colours(observed)
            if profile_hair and shot_hair and not (profile_hair & shot_hair):
                issues.append(_issue('identity_contradicts_profile',
                    f"Shot {n} gives {aid} {'/'.join(sorted(shot_hair))} hair; the profile has "
                    f"{'/'.join(sorted(profile_hair))} hair. If this is another person, make a new character.", n))
            elif (_BEARD.search(asset.get('description', '')) and _SHAVEN.search(observed)) or (
                    _SHAVEN.search(asset.get('description', '')) and _BEARD.search(observed)
                    and not _NEGATED.search(observed[:_BEARD.search(observed).start()][-40:])):
                issues.append(_issue('identity_contradicts_profile',
                    f"Shot {n} describes {aid}'s facial hair against the profile. If this is another person, "
                    f"make a new character.", n))
    return issues


def audit(inventory: dict) -> dict:
    """The same checks over a finished catalog, as counts and examples for the reviewer."""
    assets = {a['id']: a for a in inventory.get('assets') or [] if isinstance(a, dict) and a.get('id')}
    rows = sorted(((int(n), r) for n, r in (inventory.get('shots') or {}).items() if str(n).isdigit()),
                  key=lambda x: x[0])
    found = shot_issues(rows, assets)
    for a in assets.values():
        if a.get('kind') == 'character':
            found += new_state_issues(a, {s.get('key') for s in a.get('states') or []})
    by_code: dict[str, list] = {}
    for item in found:
        by_code.setdefault(item['code'], []).append(item)
    return {'counts': {k: len(v) for k, v in sorted(by_code.items())},
            'examples': [x for v in by_code.values() for x in v[:5]],
            'states_per_character': {aid: len(a.get('states') or []) for aid, a in assets.items()
                                     if a.get('kind') == 'character'}}
