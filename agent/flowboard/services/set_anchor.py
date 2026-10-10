"""Source frames that anchor a location's reference plate to the source film's own set.

A plate drawn from words alone invents its own room: windows move, doors appear, and
every reverse angle of the film then invents again. The source already shows the
room. Its widest, emptiest frame of a location becomes the plate's location anchor;
the plate prompt keeps its geography and changes only the rendering (and removes the
people — a plate is an unoccupied set).

Frames are published under a name made from their bytes, so the same frame always has
the same URL: a production job's inputs stay identical across resumes.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path

RANK = {'EWS': 6, 'WIDE': 5, 'MWS': 4, 'MS': 3, 'OTS': 2, 'MCU': 1}


def _work_dir(board: dict) -> Path | None:
    vid = board.get('sourceVideoId')
    if not vid:
        return None
    from flowboard.config import STORAGE_DIR
    work = Path(STORAGE_DIR) / 'video_analysis' / str(vid)
    return work if work.is_dir() else None


PERSON, PARTIAL, CAPTIONED = 12, 4, 8
MIN_SCORE = 25          # at least a medium-wide view with no more than one person in it
VIEW_MIN_SCORE = 0      # a reverse view: a medium shot with one person in it, or a wide one with two
# A frame in which someone is bathing or undressed is never a location anchor: it is not
# sent to the image model, and a plate drawn from it keeps its bathers.
UNDRESSED = re.compile(
    r"\b(?:bare[- ](?:chest(?:ed)?|shoulders?|torso|back|skin)|shirtless|topless|naked|nude|undress\w*"
    r"|lingerie|bra|briefs|underwear|towel|bath(?:ing)?|foam|no (?:other )?(?:clothing|garment)s?)\b", re.I)


def _undressed_states(board: dict) -> set[tuple[str, str]]:
    """``(character, state)`` pairs whose wardrobe is undressed or bathing."""
    out = set()
    for node in board.get('nodes') or []:
        d = node.get('data') or {}
        if d.get('kind') != 'character':
            continue
        item = d.get('character') or {}
        for state in item.get('states') or []:
            text = ' '.join(str(state.get(k) or '') for k in ('key', 'label', 'look', 'wardrobe'))
            if UNDRESSED.search(text.replace('-', ' ')):
                for aid in {item.get('source_asset_id'), item.get('key')} - {None}:
                    out.add((aid, state.get('key')))
    return out


def _captioned(board: dict) -> set[int]:
    """Source shots whose frames carry burned-in captions (from the vision reading)."""
    vid = board.get('sourceVideoId')
    if not vid:
        return set()
    try:
        import uuid
        from flowboard.db import get_session
        from flowboard.db.models import VideoAnalysis
        with get_session() as s:
            row = s.get(VideoAnalysis, uuid.UUID(str(vid)))
            shots = ((row.analysis or {}).get('shots') or []) if row else []
    except Exception:
        return set()
    return {int(sh['shot']) for sh in shots if (sh.get('source') or {}).get('subtitle_events')
            or str((sh.get('source') or {}).get('subtitle') or '').strip()}


def candidate_shots(board: dict, env_key: str, view: str = '', min_score: int = MIN_SCORE) -> list[tuple[int, int]]:
    """``[(score, source shot)]`` of a location's shots worth anchoring on, best first:
    wide, empty of people and free of captions. A frame dominated by people is no
    location anchor (and may carry their likeness into an empty plate). ``view`` keeps
    the shots that look along one of the set's two directions (master/reverse)."""
    characters = set()
    for node in board.get('nodes') or []:
        d = node.get('data') or {}
        if d.get('kind') == 'character':
            item = d.get('character') or {}
            characters |= {item.get('source_asset_id'), item.get('key')}
    captioned = _captioned(board)
    undressed = _undressed_states(board)
    out = []
    for node in board.get('nodes') or []:
        d = node.get('data') or {}
        if d.get('kind') != 'sequence':
            continue
        fallback = (d.get('sequence') or {}).get('environment_key')
        for shot in d.get('shots') or []:
            if (shot.get('environment_key') or fallback) != env_key or shot.get('source_shot') is None:
                continue
            if view and shot.get('set_view') != view:
                continue
            seen = [p.get('visibility') for p in shot.get('asset_presence') or [] if isinstance(p, dict)
                    and p.get('asset_id') in characters]
            shown = {p.get('asset_id') for p in shot.get('asset_presence') or [] if isinstance(p, dict)
                     and p.get('visibility') in ('visible', 'partial')}
            if any((aid, state) in undressed for aid, state in (shot.get('character_states') or {}).items()
                   if aid in shown):
                continue
            score = (RANK.get(str(shot.get('framing') or '').upper(), 0) * 10 - PERSON * seen.count('visible')
                     - PARTIAL * seen.count('partial') - (CAPTIONED if int(shot['source_shot']) in captioned else 0))
            if score >= min_score:
                out.append((score, int(shot['source_shot'])))
    return sorted(out, key=lambda x: (-x[0], x[1]))


FACE_PENALTY = 300      # per unit of frame area covered by the largest face (3% → −9)


def _face_shares(work: Path) -> dict[str, float]:
    """``{keyframe name: largest face's share of the frame}`` from the catalog's face
    fingerprints when the preparation made them (face_id cache); {} otherwise."""
    import json
    caches = sorted((work / 'face-id').glob('faces-*.json'), key=lambda p: p.stat().st_mtime) if (work / 'face-id').is_dir() else []
    if not caches:
        return {}
    try:
        faces = json.loads(caches[-1].read_text())['faces']
    except Exception:
        return {}
    shares = {}
    for eid, found in faces.items():
        if not found:
            continue
        # Evidence ids are shot-N-frame-K; frames are about 16:9 at the measured width.
        largest = max(f['box'][2] * f['box'][3] for f in found)
        shares[eid] = largest
    return shares


def frame_for(work: Path, source_shot: int) -> Path | None:
    """The middle keyframe of a source shot (current keyframe folders first)."""
    folders = sorted((p for p in work.glob('frames*') if p.is_dir()), key=lambda p: (p.name == 'frames', p.name))
    for folder in folders:
        frames = sorted(folder.glob(f'shot{int(source_shot):03d}_*.jpg'),
                        key=lambda p: int(p.stem.rsplit('_', 1)[1]) if p.stem.rsplit('_', 1)[1].isdigit() else 0)
        if frames:
            return frames[len(frames) // 2]
    return None


def publish_stable(path: Path) -> str | None:
    """Publish a file under a content-derived name: the same bytes, the same URL."""
    from flowboard.services.flowstudio import r2
    if not r2.is_configured():
        return None
    data = path.read_bytes()
    name = f'set-anchor-{hashlib.sha256(data).hexdigest()[:32]}{path.suffix.lower() or ".jpg"}'
    staged = Path(tempfile.gettempdir()) / name
    if not staged.exists():
        staged.write_bytes(data)
    try:
        return r2.upload_file(staged)
    except Exception:   # an anchor improves a plate; without one the plate is still made
        return None


def view_candidates(board: dict, env_key: str, view: str) -> list[tuple[int, int]]:
    """Anchors for one of the set's two views. A reverse is often only filmed over a
    shoulder, so its frames may hold people (the plate prompt leaves them out); the master
    prefers its strict bar, and before the shots carry views it takes every direction."""
    if view == 'reverse':
        return candidate_shots(board, env_key, 'reverse', VIEW_MIN_SCORE)
    return (candidate_shots(board, env_key, 'master') or candidate_shots(board, env_key, 'master', VIEW_MIN_SCORE)
            or candidate_shots(board, env_key))


def anchor_frames(board: dict, env_key: str, limit: int = 2, view: str = '', chosen=()) -> list[Path]:
    """The location's best anchor frames: the top candidates, re-ranked by how little of
    the frame a face covers when face fingerprints exist. ``chosen`` are the shots the scene
    staging picked as showing this view best (it saw every frame of the scene); they come
    first when they are this location's shots of that view, and never with someone undressed."""
    work = _work_dir(board)
    if work is None:
        return []
    faces = _face_shares(work)
    eligible = {n for _, n in candidate_shots(board, env_key, view, -10 ** 9)} if chosen else set()
    out = []
    for n in chosen:
        frame = frame_for(work, n) if n in eligible else None
        if frame and frame not in out and len(out) < limit:
            out.append(frame)
    ranked = []
    for score, n in (view_candidates(board, env_key, view) if view else candidate_shots(board, env_key)):
        frame = frame_for(work, n)
        if not frame:
            continue
        if faces:
            from PIL import Image
            with Image.open(frame) as im:
                area = float(im.width * im.height) or 1.0
            k = frame.stem.rsplit('_', 1)[1]
            share = faces.get(f'shot-{n}-frame-{k}', 0.0) / area
            score -= FACE_PENALTY * share
        ranked.append((score, n, frame))
    for _, _, frame in sorted(ranked, key=lambda x: (-x[0], x[1])):
        if len(out) >= limit:
            break
        if frame not in out:
            out.append(frame)
    return out


def anchor_urls(board: dict, env_item: dict, limit: int = 2, view: str = '') -> list[str]:
    key = env_item.get('source_asset_id') or env_item.get('key')
    chosen = [n for n in ((env_item.get('views') or {}).get(f'{view}_shots') or []) if isinstance(n, int)] if view else []
    return [u for u in (publish_stable(p) for p in anchor_frames(board, key, limit, view, chosen)) if u]
