"""Faces in the source keyframes as identity evidence for the catalog. Local and free.

The catalog model merges two people when nothing in its words tells them apart (two
dark-haired women; a chef in a flashback taken for the lead). A face fingerprint does:
the same person's faces agree across a film (cosine ≈ 0.75–0.85 with SFace), another
person's do not.

Only unambiguous samples count: a shot where the catalog shows exactly one character
fully visible and the keyframe holds exactly one face. For each scene a character
appears in, its faces are compared with that character's faces in the scenes before
it. A scene whose faces sit below ``FACE_MATCH`` (median) is reported — softly, like a
wardrobe note: one repair may give that person their own identity; what remains is a
reviewer note. A reference whose own faces disagree (stylised animation, masks) is not
used, so no check is better than a wrong one.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path

VERSION = 1
FACE_MATCH = 0.5        # median similarity of a scene's faces to the person's earlier faces
TIGHT = 0.65            # earlier faces' median similarity to their own mean, to be a reference
MIN_REFERENCE = 4       # earlier face samples needed before a scene is judged
_SHOT = re.compile(r"^shot-(\d+)-frame-\d+$")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def faces_for(work: Path, evidence: list[dict]) -> dict:
    """``{evidence id: [{box, score, embedding}]}`` for the supplied keyframes, cached."""
    from .face_id_worker import VERSION as worker_version, DETECTOR, RECOGNIZER
    jobs = [{'id': e['id'], 'path': str(work / e['frame'])} for e in evidence if e.get('frame')]
    if not jobs:
        return {}
    folder = work / 'face-id'
    folder.mkdir(parents=True, exist_ok=True)
    key = _digest({'jobs': jobs, 'sizes': [Path(j['path']).stat().st_size if Path(j['path']).exists() else 0 for j in jobs],
                   'worker': worker_version, 'models': [DETECTOR, RECOGNIZER]})[:24]
    out = folder / f'faces-{key}.json'
    if out.exists():
        return json.loads(out.read_text())['faces']
    jobs_file = folder / f'jobs-{key}.json'
    jobs_file.write_text(json.dumps(jobs))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'flowboard.services.video_analyzer.face_id_worker', str(out), str(jobs_file),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=max(300, len(jobs)))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError('Face fingerprinting timed out')
    if proc.returncode != 0 or not out.exists():
        tail = (err or b'').decode(errors='replace').strip().splitlines()[-3:]
        raise RuntimeError('Face fingerprinting failed: ' + ' | '.join(tail)[:400])
    return json.loads(out.read_text())['faces']


def samples(rows: list[tuple[int, dict]], faces: dict, kinds: dict) -> dict:
    """``{character: [(shot, scene, vector)]}`` from shots showing exactly one character
    fully visible, in keyframes holding exactly one face."""
    import numpy as np
    by_shot: dict[int, list[str]] = {}
    for eid in faces:
        m = _SHOT.match(eid)
        if m:
            by_shot.setdefault(int(m.group(1)), []).append(eid)
    out: dict[str, list] = {}
    for n, row in rows:
        visible = [p.get('asset_id') for p in row.get('asset_presence') or [] if isinstance(p, dict)
                   and p.get('visibility') == 'visible' and kinds.get(p.get('asset_id')) == 'character']
        if len(visible) != 1:
            continue
        for eid in by_shot.get(int(n), []):
            found = faces.get(eid) or []
            if len(found) == 1:
                out.setdefault(visible[0], []).append((int(n), row.get('scene_id'), np.array(found[0]['embedding'], dtype=np.float32)))
    return out


def _reference(vectors):
    """The mean of a person's faces, or None when they do not agree among themselves."""
    import numpy as np
    X = np.array(vectors, dtype=np.float32)
    if len(X) < MIN_REFERENCE:
        return None
    centre = X.mean(axis=0)
    centre /= np.linalg.norm(centre) or 1.0
    return centre if float(np.median(X @ centre)) >= TIGHT else None


def identity_issues(rows: list[tuple[int, dict]], faces: dict, assets: dict, *, check_from: int | None = None) -> list[dict]:
    """Scenes (from ``check_from`` on) where a character's faces are not their earlier faces."""
    import numpy as np
    from .wardrobe_checks import SOFT
    if not faces:
        return []
    kinds = {aid: a.get('kind') for aid, a in assets.items()}
    found = samples(rows, faces, kinds)
    issues = []
    for aid, items in found.items():
        scenes: list[str] = []
        for _, scene, _ in items:
            if scene not in scenes:
                scenes.append(scene)
        for scene in scenes:
            here = [(n, v) for n, s, v in items if s == scene]
            if check_from is not None and max(n for n, _ in here) < check_from:
                continue
            first = min(n for n, _ in here)
            earlier = [v for n, s, v in items if s != scene and n < first]
            centre = _reference(earlier)
            if centre is None:
                continue
            sims = np.array([v for _, v in here]) @ centre
            median = float(np.median(sims))
            if median < FACE_MATCH:
                shots = sorted({n for n, _ in here})
                issues.append({'code': 'face_contradicts_identity', 'kind': SOFT, 'shot': shots[0],
                               'message': (f"In scene {scene} (shots {', '.join(map(str, shots[:8]))}) the face shown for "
                                           f"{aid} does not match {aid}'s face in earlier scenes (similarity {median:.2f}; "
                                           f"the same person scores about 0.8). If this is another person, give them their "
                                           f"own character id in these shots instead of {aid}.")})
    return issues


def audit(inventory: dict, faces: dict) -> dict:
    """The same check over a finished catalog: every scene against all of the person's other scenes."""
    import numpy as np
    assets = {a['id']: a for a in inventory.get('assets') or [] if isinstance(a, dict) and a.get('id')}
    rows = sorted(((int(n), r) for n, r in (inventory.get('shots') or {}).items() if str(n).isdigit()), key=lambda x: x[0])
    found = samples(rows, faces, {aid: a.get('kind') for aid, a in assets.items()})
    flagged, checked = [], 0
    for aid, items in found.items():
        for scene in dict.fromkeys(s for _, s, _ in items):
            here = [v for _, s, v in items if s == scene]
            centre = _reference([v for _, s, v in items if s != scene])
            if centre is None:
                continue
            checked += 1
            median = float(np.median(np.array(here) @ centre))
            if median < FACE_MATCH:
                flagged.append({'character': aid, 'scene': scene, 'similarity': round(median, 2),
                                'shots': sorted({n for n, s, _ in items if s == scene})})
    return {'scenes_checked': checked, 'faces_not_matching': flagged}
