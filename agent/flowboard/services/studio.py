"""Reskin Studio: one source film's whole pipeline in one view, with the owner's controls
at every stage.

Everything here reads the existing records (the source analysis, the board, its jobs and the
source review) and acts through the existing pipeline; nothing generates differently. An
edit goes through the same source locks and digests the writer checks, keeps the values it
replaces (sourceVerification.source_corrections), and names the clips it reopens.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlmodel import select

from flowboard.db.models import AutomationJob, AutomationProject, VideoAnalysis
from flowboard.services import automation_jobs, production_run, reskin, reskin_review_policy as policy

# Avis Seedance 2.5 price per generated second (2026-10-01 sheet); the white-model motion
# reference adds about 13 % on a reskin clip.
SEEDANCE_USD = {'480p': 0.113, '720p': 0.254, '1080p': 0.45}
MOTION_SHARE = 0.13
DELIVERIES = ('on_camera', 'offscreen', 'voice_over')
LOCK_FIELDS = ("framing", "camera", "framing_note", "background", "staging", "action", "dialogue", "character_states")
IMAGE_MODELS = ('dola-seedream-5-0-pro', 'gemini-3-pro-image', 'gemini-3.1-flash-image', 'gemini-2.5-flash-image')
SOURCE_STAGES = {
    'queued': 'Đang chờ', 'analysis': 'Cắt shot, nghe thoại, xem hình', 'profiles': 'Lập danh mục nhân vật & bối cảnh',
    'source_refinement': 'Soát lại nguồn', 'adaptation': 'Chuẩn bị kịch bản', 'casting': 'Đổi vai', 'design': 'Thiết kế',
    'board': 'Dựng board', 'board_approved': 'Đã duyệt — đang chạy tiếp', 'reskin_review': 'Đối chiếu với nguồn', 'awaiting_review': 'Chờ bạn duyệt',
    'production': 'Sản xuất', 'blocked': 'Bị chặn', 'retry_wait': 'Chờ thử lại',
}
# Known provider failures, in words the owner can act on.
PROVIDER_HINTS = (
    ('overdue balance', 'Tài khoản BytePlus phía sau Avis đang nợ quá hạn — báo Avis kèm mã request.'),
    ('declined', 'Model từ chối viết; clip sẽ thử model dự phòng (Grok).'),
    ('empty text', 'Model trả về rỗng; thử lại thường được.'),
    ('timeout', 'Hết thời gian chờ nhà cung cấp; thử lại.'),
)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _settings(row: VideoAnalysis) -> dict:
    return dict(((row.options or {}).get('auto_production') or {}).get('settings') or {})


def _auto(row: VideoAnalysis) -> dict:
    return dict((row.options or {}).get('auto_production') or {})


def sequences(board: dict) -> list[dict]:
    return [n for n in board.get('nodes', []) if n.get('data', {}).get('kind') == 'sequence']


def _key(seq_node) -> str:
    return seq_node['data']['sequence']['key']


def _timing(text: str) -> Optional[list[float]]:
    m = re.match(r'Clip audio (-?[\d.]+)–(-?[\d.]+)s:', text or '')
    return [float(m[1]), float(m[2])] if m else None


def _frames(board: dict) -> dict:
    return {str(e.get('id')): str(e.get('frame') or '').split('/')[-1]
            for e in (board.get('sourceVerification') or {}).get('evidence') or [] if isinstance(e, dict)}


# ─────────────────────────────── jobs ───────────────────────────────

class Jobs:
    """A project's jobs indexed once by (kind, node) in creation order; superseded
    attempts are left out. A film of hundreds of clips is read in one pass."""

    def __init__(self, jobs):
        self.by: dict = {}
        for j in sorted(jobs, key=lambda j: j.created_at):
            if ':superseded-' in (j.request_key or ''):
                continue
            self.by.setdefault((j.kind, j.node_id), []).append(j)

    def all(self, kind: str, node: str) -> list[AutomationJob]:
        return self.by.get((kind, node), [])

    def latest(self, kind: str, node: str, slot: Optional[str] = None) -> Optional[AutomationJob]:
        return next((j for j in reversed(self.all(kind, node)) if slot is None or j.slot == slot), None)


def _index(jobs) -> Jobs:
    return jobs if isinstance(jobs, Jobs) else Jobs(jobs)


def _latest(jobs, kind: str, node_id: str, slot: Optional[str] = None) -> Optional[AutomationJob]:
    return _index(jobs).latest(kind, node_id, slot)


def _error(job: Optional[AutomationJob]) -> str:
    return (job.error or '')[:600] if job and job.status in ('failed', 'unknown', 'blocked') else ''


def hint(message: str) -> str:
    low = (message or '').lower()
    return next((text for needle, text in PROVIDER_HINTS if needle in low), '')


def takes(key: str, jobs) -> list[dict]:
    """Every render of one clip, newest first, with its cleaned audio and its check."""
    index = _index(jobs)
    node = 'vid:' + key
    cleaned = {(j.result or {}).get('native_url'): (j.result or {}).get('url')
               for j in index.all('audio_cleanup', node) if j.status == 'succeeded'}
    checks = {}
    for j in index.all('clip_quality', node):
        checks[(j.payload or {}).get('source_job')] = j
    out = []
    for j in reversed(index.all('clip', node)):
        url = (j.result or {}).get('url')
        if j.status != 'succeeded' or not url:
            continue
        q = checks.get(str(j.id))
        r = (q.result or {}) if q and q.status == 'succeeded' else {}
        out.append({'job_id': str(j.id), 'render_url': url, 'cleaned_url': cleaned.get(url), 'at': _iso(j.created_at),
                    'set_aside': bool((j.prepared or {}).get('set_aside')),
                    'quality': {'status': r.get('status') or (q.status if q else None),
                                'findings': [f.get('message') or f.get('code') for f in r.get('findings') or []][:6],
                                'heard': ((r.get('audio') or {}).get('heard_text') or '')[:600],
                                'speech_end_s': (r.get('audio') or {}).get('speech_end_s'),
                                'error': _error(q)} if q else None})
    return out


# Seedance's own audio by default (owner, 2026-10-12); 'cleaned' isolates the voices of one
# clip on request (music, room tone and effects go with it); 'silent' mutes it.
AUDIO_CHOICES = ('native', 'cleaned', 'silent')


def clip_state(key: str, jobs, held: dict, pending: set, video: Optional[dict] = None) -> dict:
    """Where one clip stands, from its newest jobs: done, rendering, preparing, failed,
    held, waiting; and which take it shows (the owner's choice, else the newest)."""
    index = _index(jobs)
    video = video or {}
    node = 'vid:' + key
    clip, write = index.latest('clip', node), index.latest('write', node)
    staging, motion = index.latest('plate', node, 'staging'), index.latest('motion_reference', node)
    made = takes(key, index)
    take = next((t for t in made if t['job_id'] == video.get('selectedTake')), None) or (made[0] if made else None)
    status, reason = 'waiting', ''
    newest_failure = next((j for j in sorted([x for x in (write, staging, motion, clip) if x], key=lambda j: j.created_at, reverse=True)
                           if j.status in ('failed', 'unknown', 'blocked')), None)
    if clip and clip.status in automation_jobs.ACTIVE:
        status = 'rendering'
    elif take and not (newest_failure and clip and newest_failure.created_at > clip.created_at):
        status = 'done'
    elif newest_failure:
        status, reason = 'failed', _error(newest_failure)
    elif any(j and j.status in automation_jobs.ACTIVE for j in (write, staging, motion)):
        status = 'preparing'
    elif key in held:
        status, reason = 'held', '; '.join(held[key])[:600]
    elif key in pending:
        status, reason = 'reviewing', 'Đang đối chiếu với nguồn.'
    elif write and write.status == 'succeeded':
        status = 'prepared'
    clay = (motion.result or {}) if motion and motion.status == 'succeeded' else {}
    choice = video.get('audioChoice') if video.get('audioChoice') in AUDIO_CHOICES else 'native'
    render = take['render_url'] if take else None
    cleaned = take['cleaned_url'] if take else None
    url = render if choice in ('native', 'silent') else (cleaned or render)
    stage_image = ((staging.result or {}).get('images') or [{}])[0] if staging and staging.status == 'succeeded' else {}
    return {
        'motion_url': clay.get('url'), 'motion_method': clay.get('method'),
        'camera': ((clay.get('layers') or {}).get('camera') or []),
        'staging_url': stage_image.get('reference_url') or stage_image.get('url'),
        'staging_model_used': (staging.result or {}).get('image_model_used') if staging and staging.status == 'succeeded' else None,
        'status': status, 'reason': reason, 'hint': hint(reason),
        'take_id': take['job_id'] if take else None, 'take_count': len(made), 'take_selected': bool(take and video.get('selectedTake') == take['job_id']),
        'render_url': render, 'cleaned_url': cleaned, 'url': url, 'audio_choice': choice,
        'cleaning': bool(choice == 'cleaned' and render and not cleaned and any(
            j.status in automation_jobs.ACTIVE and (j.payload or {}).get('url') == render for j in index.all('audio_cleanup', node))),
        'cleanup_error': next((_error(j) for j in reversed(index.all('audio_cleanup', node))
                               if (j.payload or {}).get('url') == render), '') if choice == 'cleaned' and render and not cleaned else '',
        'quality': take['quality'] if take else None,
        'has_prompt': bool(write and write.status == 'succeeded' and (write.result or {}).get('prompt')),
        'unknown_jobs': [{'id': str(j.id), 'kind': j.kind, 'slot': j.slot, 'error': _error(j)}
                         for j in (write, staging, motion, clip) if j and j.status == 'unknown'],
        'updated_at': _iso(max((j.updated_at for j in (write, staging, motion, clip) if j), default=None)),
    }


# ─────────────────────────────── shots ───────────────────────────────

def _heard(board: dict) -> dict[str, list[dict]]:
    """The source review's own hearing of each clip, in clip time."""
    out = {}
    for r in (board.get('reskinSourceReview') or {}).get('receipts') or []:
        heard = (r.get('observations') or {}).get('heard_dialogue') or []
        out[r.get('clip')] = [h for h in heard if isinstance(h, dict) and isinstance(h.get('text'), str)]
    return out


def shot_rows(board: dict, seq_node: dict, heard: list[dict], findings: list[dict]) -> list[dict]:
    shots = seq_node['data']['shots']
    origin = shots[0]['source_start']
    offset = max(0.0, origin - 1)  # the review excerpt starts one second early
    frames = _frames(board)
    rows = []
    for i, s in enumerate(shots, 1):
        a, b = s['source_start'] - origin, s['source_end'] - origin
        sid = f"{_key(seq_node)}:{i}"
        mine = [h for h in heard if h.get('shot_id') == sid or (not h.get('shot_id') and a - .15 <= (h['start'] + h['end']) / 2 + offset - origin < b + .15)]
        timing = [_timing(t) for t in s.get('performance') or []]
        lines = [{**{k: d.get(k) for k in ('who', 'speaker_id', 'line', 'delivery')}, 'cont': bool(d.get('cont')),
                  'timing': timing[j] if j < len(timing) else None} for j, d in enumerate(s.get('dialogue') or [])]
        heard_text = ' '.join(h['text'].strip() for h in mine).strip()
        same = policy.word_difference(policy.spoken(' '.join(l['line'] or '' for l in lines)), policy.spoken(heard_text)) in (None, 'speech_interjection', 'speech_spelling')
        rows.append({
            'index': i, 'source_shot': s.get('source_shot'), 'start': round(a, 3), 'end': round(b, 3),
            'source_start': s['source_start'], 'source_end': s['source_end'],
            'frame': next((frames[e] for e in s.get('source_evidence') or [] if frames.get(e)), None),
            'framing': s.get('framing'), 'camera': s.get('camera'), 'action': s.get('action') or [], 'staging': s.get('staging') or '',
            'characters': s.get('character_keys') or [], 'states': s.get('character_states') or {},
            'presence': [{k: x.get(k) for k in ('asset_id', 'visibility', 'state', 'position', 'holder_id')}
                         for x in s.get('asset_presence') or [] if isinstance(x, dict)],
            'lines': lines, 'heard': heard_text if heard_text and not same else '',
            'findings': [f for f in findings if f.get('shot') == sid],
        })
    return rows


VISIBILITY = ('visible', 'partial', 'offscreen', 'occluded')


def _set_presence(s: dict, rows: list, assets: dict) -> None:
    """Who and what is in the shot, in the shot record and its one source appearance;
    fields the owner did not touch (evidence, holder, containment) stay."""
    seen, fresh = set(), []
    for r in rows:
        aid = r.get('asset_id')
        if aid not in assets:
            raise ValueError('%s không có trong danh mục nguồn.' % aid)
        if aid in seen:
            raise ValueError('%s xuất hiện hai lần trong shot.' % (assets[aid].get('name') or aid))
        if r.get('visibility') not in VISIBILITY:
            raise ValueError('Mức hiện diện phải là trong khung, một phần, ngoài khung hoặc bị che.')
        seen.add(aid)
        fresh.append({k: ' '.join(str(r.get(k) or '').split()) for k in ('state', 'position')} | {'asset_id': aid, 'visibility': r['visibility'],
                     **({'holder_id': r['holder_id']} if r.get('holder_id') in assets else {})})
    def merged(old_rows, with_shot):
        old = {x.get('asset_id'): x for x in old_rows or [] if isinstance(x, dict)}
        out = []
        for row in fresh:
            base = dict(old.get(row['asset_id']) or {'contains_ids': [], 'evidence_ids': []})
            if 'holder_id' not in row:
                base.pop('holder_id', None)
            base.update(row)
            if with_shot:
                base['source_shot'] = s.get('source_shot')
            out.append(base)
        return out
    s['asset_presence'] = merged(s.get('asset_presence'), True)
    for app in s.get('source_appearances') or []:
        app['asset_presence'] = merged(app.get('asset_presence'), False)
    on = [r['asset_id'] for r in fresh if r['visibility'] in ('visible', 'partial')]
    s['character_keys'] = [a for a in on if assets[a].get('kind') == 'character']
    s['scene_present_asset_ids'] = list(dict.fromkeys([*(s.get('scene_present_asset_ids') or []), *[r['asset_id'] for r in fresh]]))


def _owner_label(user) -> str:
    return (getattr(user, 'display_name', None) or getattr(user, 'username', None) or 'owner') if user else 'owner'


def edit_shot(board: dict, source_shot: int, patch: dict, user=None) -> tuple[dict, dict]:
    """(new board, impact) for an owner edit of one source shot: its dialogue lines (words,
    speaker, delivery, timing within the clip), action beats or staging. Locks and digests
    follow; the replaced values are kept."""
    out = deepcopy(board)
    found = [(n, s) for n in sequences(out) for s in n['data']['shots'] if s.get('source_shot') == source_shot]
    if len(found) != 1:
        raise ValueError('Không tìm thấy shot nguồn %s.' % source_shot)
    seq_node, s = found[0]
    if set(patch) - {'lines', 'action', 'staging', 'presence', 'character_states'}:
        raise ValueError('Chỉ sửa được thoại, hành động, dàn cảnh, người/vật trong shot và trang phục.')
    assets = {a.get('id'): a for a in out.get('productionAssets') or []}
    origin = seq_node['data']['shots'][0]['source_start']
    a, b = s['source_start'] - origin, s['source_end'] - origin
    before = {k: deepcopy(s.get(k)) for k in ('dialogue', 'performance', 'heard', 'action', 'staging', 'source_appearances',
                                              'asset_presence', 'character_states', 'character_keys', 'scene_present_asset_ids')}
    if 'lines' in patch:
        dialogue, performance = [], []
        for line in patch['lines'] or []:
            text = ' '.join(str(line.get('line') or '').split())
            if not text:
                raise ValueError('Một câu thoại đang trống.')
            if line.get('delivery') not in DELIVERIES:
                raise ValueError('Cách nói phải là trong khung, ngoài khung hoặc voice-over.')
            sid = line.get('speaker_id') or None
            if sid and assets.get(sid, {}).get('kind') != 'character':
                raise ValueError('Người nói %s không có trong danh mục nhân vật.' % sid)
            timing = line.get('timing') or [a, b]
            x, y = float(timing[0]), float(timing[1])
            if not (a - .15 <= x < y <= b + .15):
                raise ValueError('Mốc câu "%s" phải nằm trong shot (%.2f–%.2f s của clip).' % (text[:40], a, b))
            who = str(line.get('who') or (assets.get(sid, {}).get('name') if sid else '') or 'Unidentified voice')
            dialogue.append({'who': who, 'line': text, 'delivery': line['delivery'], **({'speaker_id': sid} if sid else {}),
                             **({'cont': True} if line.get('cont') else {})})
            performance.append(f'Clip audio {x:.3f}–{y:.3f}s: ' + ('continue the line begun in the previous shot; never restart it.'
                                                                  if line.get('cont') else 'speak this line once.'))
        s['dialogue'], s['performance'] = dialogue, performance
        s['heard'] = ' '.join(d['line'] for d in dialogue)
        for app in s.get('source_appearances') or []:
            old = app.get('speakers') or []
            app['speakers'] = [{**(old[j] if j < len(old) and old[j].get('asset_id') == d.get('speaker_id') else {}),
                                'label': d['who'], 'asset_id': d.get('speaker_id'), 'delivery': d['delivery']}
                               for j, d in enumerate(dialogue)]
    if 'action' in patch:
        beats = [' '.join(str(x).split()) for x in patch['action'] or [] if str(x).strip()]
        if not beats:
            raise ValueError('Shot cần ít nhất một nhịp hành động.')
        s['action'] = beats
    if 'staging' in patch:
        s['staging'] = ' '.join(str(patch['staging'] or '').split())
    if 'presence' in patch:
        _set_presence(s, patch['presence'] or [], assets)
    if 'character_states' in patch:
        states = {}
        for aid, key in (patch['character_states'] or {}).items():
            asset = assets.get(aid) or {}
            if asset.get('kind') != 'character' or key not in {st.get('key') for st in asset.get('states') or []}:
                raise ValueError('Trang phục %s không có trong danh mục của %s.' % (key, asset.get('name') or aid))
            states[aid] = key
        s['character_states'] = states
        for app in s.get('source_appearances') or []:
            app['character_states'] = dict(states)
    sv = out.setdefault('sourceVerification', {})
    n = str(source_shot)
    lock_before = deepcopy((sv.get('locked_shots') or {}).get(n))
    if lock_before is not None:
        sv['locked_shots'][n] = {**lock_before, **{k: deepcopy(s.get(k)) for k in LOCK_FIELDS if k in lock_before or s.get(k) is not None}}
    apps = s.get('source_appearances') or []
    digest_before = (sv.get('shot_digests') or {}).get(n)
    if len(apps) == 1 and isinstance(sv.get('shot_digests'), dict):
        sv['shot_digests'][n] = digest({k: v for k, v in apps[0].items() if k != 'source_shot'})
    sv.setdefault('source_corrections', []).append({
        'at': datetime.now(timezone.utc).isoformat(), 'by': _owner_label(user), 'method': 'studio edit',
        'changes': [{'clip': _key(seq_node), 'source_shot': source_shot, 'fields': sorted(patch), 'before': before,
                     'lock_before': lock_before, 'digest_before': digest_before}]})
    return out, {'clips': [_key(seq_node)], 'review': True}


# ─────────────────────────────── materials ───────────────────────────────

def materials(board: dict, pid: str, jobs: list[AutomationJob], used: Optional[set] = None) -> list[dict]:
    """Every material sheet: its image, newest job and whether any clip uses it (``used``:
    the 'node:slot' keys the clips need; None when the shotlist is not ready)."""
    from flowboard.services import primary_materials as primary
    out = []
    for item in primary.items(board, pid):
        node = next(n for n in board['nodes'] if n['id'] == item['node_id'])
        d = node['data']
        slots = [{'slot': item['slot'], 'label': 'Thiết kế chính' if item['kind'] == 'character' else 'Góc chính'}]
        if item['kind'] == 'character':
            slots += [{'slot': st['key'], 'label': st['label']} for st in item['states']]
        elif d.get('reverse') is not None or (d.get(item['kind']) or {}).get('views'):
            slots.append({'slot': 'reverse', 'label': 'Góc ngược'})
        for sl in slots:
            plate = (d.get('states', {}).get(sl['slot']) if item['kind'] == 'character' and sl['slot'] != 'identity' else d.get(sl['slot'])) or {}
            job = _latest(jobs, 'plate', node['id'], sl['slot'])
            drawn = [j for j in reversed(_index(jobs).all('plate', node['id'])) if j.slot == sl['slot'] and j.status == 'succeeded']
            sl['versions'] = [{'job_id': str(j.id), 'url': img.get('reference_url') or img.get('url'), 'media_id': img.get('media_id') or '',
                               'at': _iso(j.created_at), 'model': (j.result or {}).get('image_model_used') or (j.payload or {}).get('image_model')}
                              for j in drawn[:8] for img in [((j.result or {}).get('images') or [{}])[0]] if img.get('reference_url') or img.get('url')]
            needed = used is None or f"{node['id']}:{sl['slot']}" in used
            sl.update(image=plate.get('referenceUrl'), uploaded=bool(plate.get('uploaded')), used=needed,
                      status=job.status if job else ('ready' if plate.get('referenceUrl') else 'missing' if needed else 'unused'),
                      error=_error(job), hint=hint(_error(job)), updated_at=_iso(job.updated_at) if job else None)
        profile = item['profile']
        out.append({'node_id': item['node_id'], 'kind': item['kind'] if item['kind'] != 'asset' else (profile.get('kind') or 'prop'),
                    'name': profile.get('production_name') or profile.get('name') or item['node_id'],
                    'role': profile.get('role') or '', 'description': profile.get('description') or profile.get('summary') or '',
                    'target_description': profile.get('target_description') or '', 'image_model': d.get('imageModel') or '',
                    'clips': item['clips'], 'required': item['required'], 'slots': slots})
    return out


def set_material(board: dict, node_id: str, *, target_description: Optional[str] = None,
                 image_model: Optional[str] = None) -> tuple[dict, dict]:
    """The owner's design note (it replaces the source look on every sheet of this material)
    and/or its image model. Returns the board and the clips the change reopens."""
    out = deepcopy(board)
    node = next((n for n in out.get('nodes', []) if n['id'] == node_id), None)
    if not node or node['data'].get('kind') not in ('character', 'environment', 'asset'):
        raise ValueError('Không tìm thấy material %s.' % node_id)
    d = node['data']
    if target_description is not None:
        text = ' '.join(target_description.split())
        if text:
            d[d['kind']]['target_description'] = text[:2000]
        else:
            d[d['kind']].pop('target_description', None)
    if image_model is not None:
        if image_model and image_model not in IMAGE_MODELS:
            raise ValueError('Model ảnh không hỗ trợ: %s' % image_model)
        if image_model:
            d['imageModel'] = image_model
        else:
            d.pop('imageModel', None)
    return out, {'clips': material_clips(out, node_id)}


def material_clips(board: dict, node_id: str) -> list[str]:
    node = next(n for n in board['nodes'] if n['id'] == node_id)
    d = node['data']; item = d[d['kind']]
    aid = item.get('source_asset_id') or item.get('id') or item.get('key')
    keys = []
    for seq in sequences(board):
        shots = seq['data']['shots']
        if any(aid in sh.get('character_keys', []) or aid == sh.get('environment_key') or
               any(p.get('asset_id') == aid for p in sh.get('asset_presence', [])) for sh in shots):
            keys.append(_key(seq))
    return keys


def use_image(board: dict, node_id: str, slot: str, url: str, media_id: str, jobs: list[AutomationJob]) -> dict:
    """Bind an image the owner chose (an upload or a library picture) to one sheet slot."""
    out = deepcopy(board)
    node = next((n for n in out.get('nodes', []) if n['id'] == node_id), None)
    if not node:
        raise ValueError('Không tìm thấy material %s.' % node_id)
    d = node['data']
    target = d.setdefault('states', {}).setdefault(slot, {}) if d['kind'] == 'character' and slot != 'identity' else d.setdefault(slot, {})
    previous = [str(j.id) for j in jobs if j.kind == 'plate' and j.node_id == node_id and j.slot == slot]
    target.update(referenceUrl=url, mediaId=media_id, uploaded=True, status='done',
                  ignoredRuntimeJobIds=sorted(set(target.get('ignoredRuntimeJobIds') or []) | set(previous)))
    target.pop('needsIdentityRefresh', None)
    return out


# ─────────────────────────────── clips ───────────────────────────────

def set_clip(board: dict, key: str, *, staging_image_model: Optional[str] = None, motion_profile: Optional[str] = None,
             manual_prompt: Optional[str] = None, end_state: Optional[str] = None, auto_prompt: Optional[dict] = None) -> dict:
    """One clip's own settings: staging image model, motion profile, and its prompt —
    the owner's own text (an independent review checks it unchanged against the source)
    or, with ``auto_prompt`` ({} or the newest written result), back to the writer."""
    out = deepcopy(board)
    node = next((n for n in out.get('nodes', []) if n['id'] == 'vid:' + key), None)
    if not node:
        raise ValueError('Không tìm thấy clip %s.' % key)
    d = node['data']
    if motion_profile is not None:
        if motion_profile and motion_profile not in reskin.MOTION_PROFILES:
            raise ValueError('Kiểu chuyển động không hỗ trợ: %s' % motion_profile)
        if motion_profile:
            d['motionProfile'] = motion_profile
        else:
            d.pop('motionProfile', None)
    if manual_prompt is not None:
        text = manual_prompt.strip()
        if len(text) < 200:
            raise ValueError('Prompt tự viết quá ngắn; dùng prompt tự động làm điểm xuất phát rồi sửa.')
        d.update(promptBy='manual', prompt=text, endState=(end_state if end_state is not None else d.get('endState')) or '')
    elif auto_prompt is not None:
        if auto_prompt.get('prompt'):
            d.update(promptBy=auto_prompt.get('writer') or 'writer', prompt=auto_prompt['prompt'], endState=auto_prompt.get('end_state') or '')
        else:
            for k in ('promptBy', 'prompt', 'endState'):
                d.pop(k, None)
    if staging_image_model is not None:
        if staging_image_model and staging_image_model not in IMAGE_MODELS:
            raise ValueError('Model ảnh không hỗ trợ: %s' % staging_image_model)
        if staging_image_model:
            node['data']['stagingImageModel'] = staging_image_model
        else:
            node['data'].pop('stagingImageModel', None)
    return out


# The fields a clip's video node keeps when its shots change; everything written or rendered
# for the old shots is left to the job history.
_KEEP_VIDEO = ('kind', 'label', 'title', 'refs', 'sequenceKey', 'stagingImageModel', 'motionProfile')


def _sequence_fields(sequence: dict, shots: list[dict]) -> dict:
    """A clip's sequence record recomputed from its shots (identity fields kept)."""
    def mmss(s):
        return f'{int(s // 60):02d}:{s % 60:05.2f}'
    chars, assets = [], []
    for s in shots:
        chars += [k for k in s.get('character_keys') or [] if k not in chars]
        assets += [k for k in s.get('asset_keys') or [] if k not in assets]
    nums = [s.get('source_shot') for s in shots]
    return {**sequence, 'duration_s': round(sum(float(s.get('duration_s') or 0) for s in shots), 3),
            'source_range': f"{mmss(shots[0]['source_start'])}–{mmss(shots[-1]['source_end'])} · shots {min(nums):03d}–{max(nums):03d}",
            'character_keys': chars, 'asset_keys': assets, 'environment_key': shots[0].get('environment_key') or sequence.get('environment_key')}


def _reset_video(data: dict, key: str, label: str) -> dict:
    return {**{k: data[k] for k in _KEEP_VIDEO if k in data}, 'kind': 'video', 'label': label, 'sequenceKey': key}


def split_clip(board: dict, key: str, at_shot: int) -> tuple[dict, dict]:
    """Two clips from one: shots before ``at_shot`` (1-based) keep the key, the rest
    become a new clip right after it. Both need a source review and a new render."""
    out = deepcopy(board)
    nodes = out['nodes']
    i = next((n for n, x in enumerate(nodes) if x['id'] == 'seq:' + key), None)
    if i is None:
        raise ValueError('Không tìm thấy clip %s.' % key)
    seq = nodes[i]
    shots = seq['data']['shots']
    if not 1 < at_shot <= len(shots):
        raise ValueError('Chọn một shot từ thứ 2 tới thứ %d để tách.' % len(shots))
    taken = {_key(n) for n in sequences(out)}
    new = next(f'{key}{c}' for c in 'bcdefghijklmnopqrstuvwxyz' if f'{key}{c}' not in taken)
    label = seq['data']['sequence'].get('label') or key
    new_label = label + new[len(key):].upper()
    first, second = shots[:at_shot - 1], shots[at_shot - 1:]
    seq['data']['shots'] = first
    seq['data']['sequence'] = _sequence_fields(seq['data']['sequence'], first)
    twin = deepcopy(seq)
    twin['id'] = 'seq:' + new
    twin['data']['shots'] = second
    twin['data']['sequence'] = _sequence_fields({**seq['data']['sequence'], 'key': new, 'label': new_label}, second)
    if 'position' in twin:
        twin['position'] = {**twin['position'], 'y': twin['position'].get('y', 0) + 320}
    video = next(x for x in nodes if x['id'] == 'vid:' + key)
    video['data'] = _reset_video(video['data'], key, label)
    twin_video = {**deepcopy(video), 'id': 'vid:' + new, 'data': _reset_video(video['data'], new, new_label)}
    if 'position' in twin_video:
        twin_video['position'] = {**twin_video['position'], 'y': twin_video['position'].get('y', 0) + 320}
    # Film order is the order of the sequence nodes: the new clip follows its source clip.
    nodes.insert(i + 1, twin)
    nodes.insert(next(n for n, x in enumerate(nodes) if x['id'] == 'vid:' + key) + 1, twin_video)
    edges = out.setdefault('edges', [])
    for e in [e for e in edges if e.get('target') == 'vid:' + key and e.get('source') != 'seq:' + key]:
        edges.append({**e, 'id': f"e-{e['source']}-vid:{new}", 'target': 'vid:' + new})
    edges += [{'id': f'e-script-seq:{new}', 'source': 'script', 'target': 'seq:' + new},
              {'id': f'e-seq:{new}-vid:{new}', 'source': 'seq:' + new, 'target': 'vid:' + new}]
    return out, {'clips': [key, new], 'review': True}


def merge_clips(board: dict, key: str) -> tuple[dict, dict]:
    """One clip from this one and the next in film order, when their shots are
    contiguous and the whole fits one reskin clip."""
    out = deepcopy(board)
    seqs = sequences(out)
    pos = next((n for n, x in enumerate(seqs) if _key(x) == key), None)
    if pos is None:
        raise ValueError('Không tìm thấy clip %s.' % key)
    if pos + 1 >= len(seqs):
        raise ValueError('Đây là clip cuối; không có clip sau để gộp.')
    a, b = seqs[pos], seqs[pos + 1]
    other = _key(b)
    shots = a['data']['shots'] + b['data']['shots']
    if abs(b['data']['shots'][0]['source_start'] - a['data']['shots'][-1]['source_end']) > .05:
        raise ValueError('Hai clip không liền nhau trên video gốc.')
    limit = reskin.max_seconds(out) if reskin.enabled(out) else 30.0
    total = shots[-1]['source_end'] - shots[0]['source_start']
    if total > limit + .001:
        raise ValueError('Gộp lại dài %.1f s, quá giới hạn %g s của một clip reskin.' % (total, limit))
    a['data']['shots'] = shots
    a['data']['sequence'] = _sequence_fields(a['data']['sequence'], shots)
    video = next(n for n in out['nodes'] if n['id'] == 'vid:' + key)
    video['data'] = _reset_video(video['data'], key, a['data']['sequence'].get('label') or key)
    gone = {'seq:' + other, 'vid:' + other}
    out['nodes'] = [n for n in out['nodes'] if n['id'] not in gone]
    out['edges'] = [e for e in out.get('edges', []) if e.get('source') not in gone and e.get('target') not in gone]
    return out, {'clips': [key], 'removed': [other], 'review': True}


def _mmss(s: float) -> str:
    return f'{int(s // 60):02d}:{s % 60:05.2f}'


def move_cut(board: dict, source_shot: int, at: float) -> tuple[dict, dict]:
    """Move the cut after one source shot to source time ``at``: this shot ends there
    and the next one (possibly the first of the next clip) starts there. Lines must
    already fall on their side; the next clip's own timings follow its new start."""
    out = deepcopy(board)
    order = [(n, i, s) for n in sequences(out) for i, s in enumerate(n['data']['shots'])]
    pos = next((k for k, (_, _, s) in enumerate(order) if s.get('source_shot') == source_shot), None)
    if pos is None:
        raise ValueError('Không tìm thấy shot nguồn %s.' % source_shot)
    if pos + 1 >= len(order):
        raise ValueError('Shot cuối phim không có điểm cắt phía sau.')
    (sa, ia, a), (sb, ib, b) = order[pos], order[pos + 1]
    if abs(a['source_end'] - b['source_start']) > .05:
        raise ValueError('Hai shot này không liền nhau trên video gốc.')
    at = round(float(at), 3)
    if not (a['source_start'] + .3 <= at <= b['source_end'] - .3):
        raise ValueError('Điểm cắt phải cách đầu shot này và cuối shot sau ít nhất 0,3 s.')
    old = a['source_end']
    a_origin = sa['data']['shots'][0]['source_start']
    for line_timing in (_timing(x) for x in a.get('performance') or []):
        if line_timing and line_timing[1] > at - a_origin + .15:
            raise ValueError('Một câu thoại của shot này kéo dài qua điểm cắt mới; sửa mốc câu đó trước.')
    b_first = ib == 0 and sb is not sa
    b_origin_old = sb['data']['shots'][0]['source_start']
    b_origin_new = at if b_first else b_origin_old
    for line_timing in (_timing(x) for x in b.get('performance') or []):
        if line_timing and line_timing[0] + b_origin_old < at - .15:
            raise ValueError('Một câu thoại của shot sau bắt đầu trước điểm cắt mới; sửa mốc câu đó trước.')
    a['source_end'] = at
    a['duration_s'] = round(at - a['source_start'], 3)
    b['source_start'] = at
    b['duration_s'] = round(b['source_end'] - at, 3)
    for s in (a, b):
        if s.get('source_tc'):
            s['source_tc'] = f"{_mmss(s['source_start'])}–{_mmss(s['source_end'])}"
    if b_first and abs(b_origin_new - b_origin_old) > 1e-6:
        # Every timing of the next clip is clip-relative: its clip now starts at the new cut.
        shift = b_origin_old - b_origin_new
        for s in sb['data']['shots']:
            s['performance'] = [re.sub(r'Clip audio (-?[\d.]+)–(-?[\d.]+)s:',
                                       lambda m: f'Clip audio {float(m[1]) + shift:.3f}–{float(m[2]) + shift:.3f}s:', x)
                                for x in s.get('performance') or []]
    limit = reskin.max_seconds(out) if reskin.enabled(out) else 30.0
    for seq in {id(sa): sa, id(sb): sb}.values():
        shots = seq['data']['shots']
        if shots[-1]['source_end'] - shots[0]['source_start'] > limit + .001:
            raise ValueError('Clip %s sẽ dài quá %g s.' % (_key(seq), limit))
        seq['data']['sequence'] = _sequence_fields(seq['data']['sequence'], shots)
    sv = out.get('sourceVerification') or {}
    for s in (a, b):
        n = str(s.get('source_shot'))
        if n in (sv.get('locked_shots') or {}):
            sv['locked_shots'][n]['duration_s'] = s['duration_s']
    clips = list(dict.fromkeys([_key(sa), _key(sb)]))
    return out, {'clips': clips, 'review': True, 'moved': {'from': old, 'to': at}}


# ─────────────────────────────── history ───────────────────────────────

def _nodes(board: dict) -> dict:
    return {n['id']: (i, n) for i, n in enumerate(board.get('nodes', []))}


def change_entry(before: dict, after: dict, kind: str, summary_text: str, clips: list, user=None) -> Optional[dict]:
    """What one studio action changed — the nodes, their edges and the source locks — as
    the values to put back on undo, and a digest of the result to check nothing changed since."""
    b, a = _nodes(before), _nodes(after)
    ids = sorted({i for i in set(b) | set(a) if (b.get(i) or (0, None))[1] != (a.get(i) or (0, None))[1]})
    sv_b, sv_a = before.get('sourceVerification') or {}, after.get('sourceVerification') or {}
    locks = sorted({n for field in ('locked_shots', 'shot_digests')
                    for n in set(sv_b.get(field) or {}) | set(sv_a.get(field) or {})
                    if (sv_b.get(field) or {}).get(n) != (sv_a.get(field) or {}).get(n)})
    if not ids and not locks:
        return None
    touch = lambda e, keep: e.get('source') in keep or e.get('target') in keep
    return {'id': uuid.uuid4().hex[:12], 'at': datetime.now(timezone.utc).isoformat(), 'by': _owner_label(user), 'kind': kind,
            'summary': summary_text[:300], 'clips': clips,
            'before': {'nodes': {i: (deepcopy(b[i][1]), b[i][0]) if i in b else None for i in ids},
                       'edges': [deepcopy(e) for e in before.get('edges', []) if touch(e, set(ids))],
                       'locks': {n: [deepcopy((sv_b.get('locked_shots') or {}).get(n)), (sv_b.get('shot_digests') or {}).get(n)] for n in locks}},
            'after_digest': _state_digest(after, ids, locks)}


def _state_digest(board: dict, ids, locks) -> str:
    nodes = _nodes(board)
    sv = board.get('sourceVerification') or {}
    return digest({'nodes': {i: (nodes[i][1] if i in nodes else None) for i in ids},
                   'locks': {n: [(sv.get('locked_shots') or {}).get(n), (sv.get('shot_digests') or {}).get(n)] for n in locks}})


def undo(board: dict, entry: dict) -> dict:
    """The board with one recorded change put back, if what it changed is still as it left it."""
    before = entry['before']
    ids, locks = list(before['nodes']), list(before['locks'])
    if _state_digest(board, ids, locks) != entry['after_digest']:
        raise ValueError('Phần này đã được sửa tiếp sau thay đổi đó; hoàn tác các thay đổi mới hơn trước.')
    out = deepcopy(board)
    keep = set(ids)
    nodes = [n for n in out.get('nodes', []) if n['id'] not in keep]
    for i, value in sorted(((i, v) for i, v in before['nodes'].items() if v), key=lambda x: x[1][1]):
        node, index = value
        nodes.insert(min(index, len(nodes)), deepcopy(node))
    out['nodes'] = nodes
    out['edges'] = [e for e in out.get('edges', []) if e.get('source') not in keep and e.get('target') not in keep] + deepcopy(before['edges'])
    sv = out.setdefault('sourceVerification', {})
    for n, (lock, dig) in before['locks'].items():
        for field, value in (('locked_shots', lock), ('shot_digests', dig)):
            if value is None:
                (sv.get(field) or {}).pop(n, None)
            else:
                sv.setdefault(field, {})[n] = deepcopy(value)
    return out


# ─────────────────────────────── settings ───────────────────────────────

# Settings that change production only (never the analysis or the board's shape).
PRODUCTION_SETTINGS = {'resolution': ('480p', '720p'), 'timing_policy': ('full_take', 'source_duration')}
GATE_SETTINGS = ('pause_for_review', 'prepare_before_render', 'review_masters')


def apply_settings(settings: dict, board: dict, patch: dict) -> tuple[dict, dict, list[str]]:
    """(settings, board, notes) after the owner changes a film's production settings."""
    s, b, notes = dict(settings), deepcopy(board), []
    for key, allowed in PRODUCTION_SETTINGS.items():
        if key in patch:
            if patch[key] not in allowed:
                raise ValueError('Giá trị %s không hợp lệ: %s' % (key, patch[key]))
            s[key] = patch[key]
    for key in GATE_SETTINGS:
        if key in patch:
            s[key] = bool(patch[key])
    if 'motion_profile' in patch:
        if patch['motion_profile'] not in reskin.MOTION_PROFILES:
            raise ValueError('Kiểu chuyển động không hỗ trợ.')
        s['motion_profile'] = patch['motion_profile']
        if patch['motion_profile'] == 'standard':
            b.pop('motionProfile', None)
        else:
            b['motionProfile'] = patch['motion_profile']
        notes.append('Clip chưa đặt kiểu riêng sẽ dựng lại clay ở lượt render tới.')
    if 'image_fallback_model' in patch:
        model = patch['image_fallback_model'] or ''
        if model and model not in IMAGE_MODELS:
            raise ValueError('Model ảnh không hỗ trợ.')
        s['image_fallback_model'] = model
        b['imageFallbackModel'] = model
        if not model:
            b.pop('imageFallbackModel', None)
    if 'budget_usd' in patch:
        cap = patch['budget_usd']
        if cap is not None and (not isinstance(cap, (int, float)) or cap < 0):
            raise ValueError('Trần chi phí phải là số đô-la không âm.')
        s['budget_usd'] = cap
        if cap:
            b['budgetUsd'] = float(cap)
        else:
            b.pop('budgetUsd', None)
    if 'style_note' in patch:
        note = ' '.join(str(patch['style_note'] or '').split())[:2000]
        s['style_note'] = note
        if note:
            b['styleNote'] = note
        else:
            b.pop('styleNote', None)
        notes.append('Ghi chú phong cách đổi thì mọi sheet được vẽ lại ở lượt chạy tới, và mọi clip cần render lại.')
    if 'style_image_url' in patch:
        url = str(patch['style_image_url'] or '').strip()
        s['style_image_url'] = url
        if url:
            b['styleImageUrl'] = url
        else:
            b.pop('styleImageUrl', None)
        notes.append('Ảnh mẫu phong cách đổi thì mọi sheet được vẽ lại ở lượt chạy tới, và mọi clip cần render lại.')
    return s, b, notes


def set_audio_and_take(board: dict, key: str, *, audio_choice: Optional[str] = None, take_id: Optional[str] = None,
                       known_takes: Optional[set] = None) -> dict:
    out = deepcopy(board)
    node = next((n for n in out.get('nodes', []) if n['id'] == 'vid:' + key), None)
    if not node:
        raise ValueError('Không tìm thấy clip %s.' % key)
    d = node['data']
    if audio_choice is not None:
        if audio_choice not in AUDIO_CHOICES:
            raise ValueError('Âm thanh phải là đã lọc nhạc, bản gốc của Seedance hoặc im lặng.')
        d['audioChoice'] = audio_choice
    if take_id is not None:
        if take_id and known_takes is not None and take_id not in known_takes:
            raise ValueError('Không có bản gen này của clip.')
        if take_id:
            d['selectedTake'] = take_id
        else:
            d.pop('selectedTake', None)
    return out


def cleanup_job(session, board: dict, pid, key: str, jobs) -> Optional[AutomationJob]:
    """A music cleanup of the clip's chosen take, when the owner chose 'cleaned' and that
    take has none yet (made or running). Returns the new job, else None."""
    from flowboard.services import reskin_audio
    index = _index(jobs)
    video = next((n.get('data') or {} for n in board.get('nodes', []) if n.get('id') == 'vid:' + key), {})
    made = takes(key, index)
    take = next((t for t in made if t['job_id'] == video.get('selectedTake')), None) or (made[0] if made else None)
    if not take or take['cleaned_url']:
        return None
    if any((j.payload or {}).get('url') == take['render_url'] and j.status in automation_jobs.ACTIVE | {'succeeded'}
           for j in index.all('audio_cleanup', 'vid:' + key)):
        return None
    seq = next(n for n in sequences(board) if _key(n) == key)
    job = AutomationJob(project_id=uuid.UUID(str(pid)), kind='audio_cleanup', node_id='vid:' + key, slot='audio',
                        request_key=f"studio-audio:{key}:{take['job_id']}:{int(datetime.now(timezone.utc).timestamp())}",
                        payload={'version': reskin_audio.VERSION, 'source_job': take['job_id'], 'url': take['render_url'],
                                 'has_dialogue': any(d.get('line') for s in seq['data']['shots'] for d in s.get('dialogue') or [])})
    session.add(job)
    return job


def quality_payload(board: dict, pid: str, key: str, take: dict, timing_policy: str) -> dict:
    """A new automatic check of one take, the same request a run makes."""
    from flowboard.services import production_quality as quality
    package = production_run.selected_packages(board, pid, [key])[0]
    return {'version': quality.VERSION, 'model': quality.MODEL, 'source_job': take['job_id'],
            'url': take['cleaned_url'] or take['render_url'], 'package': package, 'timing_policy': timing_policy}


def run_config(settings: dict, keys: list[str], mode: str) -> dict:
    from flowboard.routes.automation import ProductionRunConfig
    reskin_film = settings.get('pipeline') == 'reskin_v1'
    return ProductionRunConfig(mode=mode, sequence_keys=keys, review_masters=False, assemble=False,
                               resolution=settings.get('resolution', '480p'), timing_policy=settings.get('timing_policy', 'full_take'),
                               audio_cleanup=False, set_continuity=not reskin_film,
                               image_parallel=settings.get('image_parallel', 16), video_parallel=settings.get('video_parallel', 16),
                               max_images=settings.get('max_images', 500), max_videos=settings.get('max_videos', 500),
                               max_text=10000).model_dump()


def supersede_failed(session, pid: uuid.UUID, keys: list[str]) -> list[str]:
    """Failed jobs of these clips step aside so a new run asks again (they stay on record)."""
    stamp = str(int(datetime.now(timezone.utc).timestamp()))
    nodes = {'vid:' + k for k in keys}
    done = []
    for j in session.exec(select(AutomationJob).where(AutomationJob.project_id == pid, AutomationJob.status.in_(['failed', 'blocked']))).all():
        if j.node_id in nodes and j.kind in ('write', 'clip', 'clip_quality', 'plate', 'motion_reference', 'audio_cleanup') \
                and ':superseded-' not in j.request_key:
            j.request_key += ':superseded-' + stamp
            session.add(j)
            done.append(f'{j.kind}:{j.node_id}')
    return done


def set_aside_takes(session, pid: uuid.UUID, keys: list[str]) -> list[str]:
    """Each made take of these clips steps aside so the next render makes a new one; the
    takes stay on record to compare and choose from."""
    stamp = str(int(datetime.now(timezone.utc).timestamp()))
    nodes = {'vid:' + k for k in keys}
    done = []
    for j in session.exec(select(AutomationJob).where(AutomationJob.project_id == pid, AutomationJob.kind == 'clip',
                                                      AutomationJob.status == 'succeeded')).all():
        if j.node_id in nodes and not (j.prepared or {}).get('set_aside'):
            j.prepared = {**(j.prepared or {}), 'set_aside': True, 'set_aside_at': stamp}
            j.request_key = (j.request_key or '') + ':take-' + stamp
            session.add(j)
            done.append(str(j.id))
    return done


def clip_seconds(seq_node) -> float:
    shots = seq_node['data']['shots']
    return round(shots[-1]['source_end'] - shots[0]['source_start'], 3)


def estimate(settings: dict, seconds: float) -> float:
    return round(seconds * SEEDANCE_USD.get(settings.get('resolution', '480p'), SEEDANCE_USD['480p']) * (1 + MOTION_SHARE), 2)


# ─────────────────────────────── health and cost ───────────────────────────────

def providers(session, hours: int = 12) -> list[dict]:
    """The newest outcome of each paid service, across every project."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    jobs = session.exec(select(AutomationJob).where(AutomationJob.updated_at >= since,
                                                    AutomationJob.kind.in_(['clip', 'plate', 'write', 'clip_quality']),
                                                    AutomationJob.status.in_(['succeeded', 'failed', 'unknown']))).all()
    def service(j):
        if j.kind == 'clip':
            return 'Seedance (Avis)'
        if j.kind == 'plate':
            model = str((j.payload or {}).get('image_model') or '')
            return 'Gemini ảnh (Atrium)' if model.startswith('gemini') else 'Seedream (Avis)'
        return 'Viết prompt (Avis)' if j.kind == 'write' else 'Kiểm tra chất lượng'
    newest = {}
    for j in jobs:
        name = service(j)
        if name not in newest or j.updated_at > newest[name].updated_at:
            newest[name] = j
    out = []
    for name in ('Seedance (Avis)', 'Seedream (Avis)', 'Gemini ảnh (Atrium)', 'Viết prompt (Avis)', 'Kiểm tra chất lượng'):
        j = newest.get(name)
        if not j:
            out.append({'name': name, 'status': 'idle', 'at': None, 'message': '', 'hint': ''})
            continue
        # A writer's refusal of one clip's content is not a service outage.
        failing = j.status != 'succeeded' and not (j.kind == 'write' and '422' in (j.error or ''))
        out.append({'name': name, 'status': 'failing' if failing else 'ok', 'at': _iso(j.updated_at),
                    'message': (j.error or '')[:300] if failing else '', 'hint': hint(j.error or '') if failing else ''})
    return out


def cost(settings: dict, jobs: list[AutomationJob], seconds_of: dict, budget=None) -> dict:
    """Seedance spend from every render's own length and resolution (set-aside takes
    included: they were paid for), and what is left under the owner's cap."""
    made = [j for j in jobs if j.kind == 'clip' and j.status == 'succeeded']
    seconds = sum(float((j.payload or {}).get('duration_seconds') or seconds_of.get((j.node_id or '')[4:]) or 0) for j in made)
    usd = sum(production_run.seedance_usd({**(j.payload or {}), 'duration_seconds': (j.payload or {}).get('duration_seconds')
                                            or seconds_of.get((j.node_id or '')[4:]) or 0}) for j in made)
    return {'clips_rendered': len(made), 'seconds': round(seconds, 1), 'seedance_usd': round(usd, 2),
            'budget_usd': float(budget) if budget else None, 'left_usd': round(float(budget) - usd, 2) if budget else None,
            'images': sum(1 for j in jobs if j.kind == 'plate' and j.status == 'succeeded'),
            'writes': sum(1 for j in jobs if j.kind == 'write' and j.status == 'succeeded'),
            'note': 'Ước tính theo giá Seedance 2.5 trên Avis (cộng ~13% video chuyển động); ảnh và chữ chưa tính.'}


# ─────────────────────────────── the whole film ───────────────────────────────

def summary(session, row: VideoAnalysis) -> dict:
    auto = _auto(row)
    return {'id': str(row.id), 'name': row.name, 'project_id': str(row.automation_project_id) if row.automation_project_id else None,
            'status': row.status, 'stage': auto.get('stage'), 'stage_label': SOURCE_STAGES.get(auto.get('stage') or '', auto.get('stage') or ''),
            'error': row.error or auto.get('error'), 'duration': row.duration if hasattr(row, 'duration') else None,
            'settings': _settings(row), 'updated_at': _iso(row.updated_at), 'created_at': _iso(getattr(row, 'created_at', None)),
            'progress': row.progress or {}}


def context(session, row: VideoAnalysis) -> Optional[dict]:
    """Everything the overview and one clip's page read, built once per request."""
    project = session.get(AutomationProject, row.automation_project_id) if row.automation_project_id else None
    if not project or not sequences(project.board or {}):
        return {'project': project} if project else None
    board = automation_jobs.project_board(session, project)
    rows = session.exec(select(AutomationJob).where(AutomationJob.project_id == project.id)).all()
    pid = str(project.id)
    used = None
    try:
        packages = production_run.selected_packages(board, pid, [])
        shared, held, pending = reskin.review_state(board, packages)
        used = set(production_run.material_specs(board, packages))
    except ValueError as exc:
        shared, held, pending = [str(exc)], {}, set()
    review = board.get('reskinSourceReview') or {}
    accepted = policy.accepted(board)
    findings = [{**{k: f.get(k) for k in ('clip', 'shot', 'key', 'origin', 'criterion', 'message', 'expected', 'heard')},
                 'severity': policy.severity(f), 'accepted': f.get('key') in accepted}
                for f in review.get('findings') or [] if isinstance(f, dict)]
    videos = {n['id'][4:]: n.get('data') or {} for n in board.get('nodes', []) if str(n.get('id', '')).startswith('vid:')}
    return {'project': project, 'board': board, 'rows': rows, 'jobs': Jobs(rows), 'pid': pid, 'used': used,
            'shared': shared, 'held': held, 'pending': pending, 'review': review, 'findings': findings,
            'heard': _heard(board), 'videos': videos, 'settings': _settings(row), 'auto': _auto(row)}


def _clip_summary(ctx: dict, seq: dict) -> dict:
    key = _key(seq)
    shots = seq['data']['shots']
    settings, video = ctx['settings'], ctx['videos'].get(key, {})
    mine = [f for f in ctx['findings'] if f.get('clip') == key]
    lines = [d.get('line') or '' for s in shots for d in s.get('dialogue') or []]
    heard = ' '.join(h['text'].strip() for h in ctx['heard'].get(key, [])).strip()
    differs = bool(heard) and policy.word_difference(policy.spoken(' '.join(lines)), policy.spoken(heard)) not in (
        None, 'speech_interjection', 'speech_spelling', 'speech_boundary')
    out = {'key': key, 'label': seq['data']['sequence'].get('label') or key,
           'source_start': shots[0]['source_start'], 'source_end': shots[-1]['source_end'], 'seconds': clip_seconds(seq),
           'estimate_usd': estimate(settings, clip_seconds(seq)), 'staging_image_model': video.get('stagingImageModel') or '',
           'motion_profile': reskin.motion_profile(ctx['board'], key), 'prompt_by': video.get('promptBy') or '',
           'shot_count': len(shots), 'line_count': len(lines), 'dialogue_text': ' '.join(lines)[:2000], 'heard_differs': differs,
           'blocking': [f for f in mine if f['severity'] == 'blocking' and not f['accepted']],
           'finding_count': len(mine),
           **clip_state(key, ctx['jobs'], ctx['held'], ctx['pending'], video)}
    mark = (ctx['auto'].get('clip_approvals') or {}).get(key) or {}
    out['approval'] = mark if mark.get('url') and mark.get('url') == out['url'] else None
    return out


def detail(session, row: VideoAnalysis) -> dict:
    """The film overview: every clip as a summary (its shots load with the clip)."""
    out = summary(session, row)
    auto, settings = _auto(row), _settings(row)
    out['gates'] = {'pause_for_review': bool(settings.get('pause_for_review')), 'prepare_before_render': bool(settings.get('prepare_before_render')),
                    'review_masters': bool(settings.get('review_masters')), 'board_approved': bool(auto.get('board_approved'))}
    out['providers'] = providers(session)
    out['films'] = auto.get('studio_films') or []
    out['exports'] = auto.get('studio_exports') or []
    out['history'] = [{k: v for k, v in h.items() if k not in ('before', 'after_digest')} for h in (auto.get('studio_history') or [])][:40]
    ctx = context(session, row)
    if not ctx or 'board' not in ctx:
        project = (ctx or {}).get('project')
        out.update(stations=_stations(row, None, [], [], [], {}), clips=[], materials=[], cost=None, inbox=_inbox(row, [], [], []),
                   revision=project.revision if project else None, runs=[], assets=[])
        return out
    board, rows = ctx['board'], ctx['rows']
    clips = [_clip_summary(ctx, seq) for seq in sequences(board)]
    mats = materials(board, ctx['pid'], ctx['jobs'], ctx['used'])
    runs = sorted((j for j in rows if j.kind == 'production_run'), key=lambda j: j.created_at, reverse=True)[:8]
    review = ctx['review']
    out.update(revision=ctx['project'].revision, aspect_ratio=board.get('aspectRatio'), style=board.get('style'), image_model=board.get('imageModel'),
               board_settings={'imageFallbackModel': board.get('imageFallbackModel') or '', 'budgetUsd': board.get('budgetUsd'),
                               'styleNote': board.get('styleNote') or '', 'styleImageUrl': board.get('styleImageUrl') or '',
                               'motionProfile': board.get('motionProfile') or 'standard'},
               shared_issues=ctx['shared'], clips=clips, materials=mats, assets=assets(board),
               characters=[{'id': a['id'], 'name': a['name']} for a in assets(board) if a['kind'] == 'character'],
               review={'status': review.get('status'), 'in_progress': reskin.review_in_progress(board), 'counts': review.get('counts') or {}},
               cost=cost(settings, rows, {c['key']: c['seconds'] for c in clips}, board.get('budgetUsd')),
               runs=[{'id': str(j.id), 'status': j.status, 'stage': (j.result or {}).get('stage'), 'created_at': _iso(j.created_at),
                      'keys': ((j.payload or {}).get('config') or {}).get('sequence_keys') or [], 'mode': ((j.payload or {}).get('config') or {}).get('mode'),
                      'errors': [str(e)[:300] for e in ((j.result or {}).get('errors') or [])][:4]} for j in runs],
               stations=_stations(row, board, clips, mats, ctx['findings'], review),
               inbox=_inbox(row, clips, mats, ctx['findings']))
    return out


def clip_detail(session, row: VideoAnalysis, key: str) -> dict:
    """One clip in full: its shots (dialogue, action, who is on screen and in what), its
    findings and every take."""
    ctx = context(session, row)
    if not ctx or 'board' not in ctx:
        raise ValueError('Phim chưa có shotlist.')
    seq = next((n for n in sequences(ctx['board']) if _key(n) == key), None)
    if not seq:
        raise ValueError('Không tìm thấy clip %s.' % key)
    mine = [f for f in ctx['findings'] if f.get('clip') == key]
    return {**_clip_summary(ctx, seq), 'shots': shot_rows(ctx['board'], seq, ctx['heard'].get(key, []), mine),
            'findings': [f for f in mine if not f.get('shot')], 'takes': takes(key, ctx['jobs']),
            'revision': ctx['project'].revision}


def assets(board: dict) -> list[dict]:
    """The source catalog the shot presence editor picks from."""
    return [{'id': a.get('id'), 'name': a.get('production_name') or a.get('name') or a.get('id'), 'kind': a.get('kind'),
             'states': [{'key': s.get('key'), 'label': s.get('label') or s.get('key')} for s in a.get('states') or []]}
            for a in board.get('productionAssets') or [] if a.get('id')]


def needs_review(clip: dict) -> bool:
    """A made clip whose automatic check did not pass and the owner has not approved."""
    return (clip['status'] == 'done' and not clip.get('approval')
            and (clip.get('quality') or {}).get('status') not in ('checks_passed', None))


def latest_written(session, project_id, key: str, *, manual: Optional[bool] = None) -> Optional[dict]:
    jobs = [j for j in session.exec(select(AutomationJob).where(AutomationJob.project_id == project_id, AutomationJob.kind == 'write',
                                                                AutomationJob.node_id == 'vid:' + key, AutomationJob.status == 'succeeded')).all()
            if manual is None or ((j.result or {}).get('writer') == 'manual') == manual]
    if not jobs:
        return None
    j = max(jobs, key=lambda j: j.created_at)
    r = j.result or {}
    return {'prompt': r.get('prompt'), 'end_state': r.get('end_state'), 'writer': r.get('writer'),
            'warnings': r.get('warnings') or [], 'at': _iso(j.updated_at)}


def prompt(session, project_id, key: str, board: Optional[dict] = None) -> Optional[dict]:
    """The clip's prompt: the owner's own text when it is in manual mode, else the newest written one."""
    video = next((n.get('data') or {} for n in (board or {}).get('nodes', []) if n.get('id') == 'vid:' + key), {})
    written = latest_written(session, project_id, key)
    if video.get('promptBy') == 'manual':
        return {'prompt': video.get('prompt') or '', 'end_state': video.get('endState') or '', 'writer': 'manual', 'manual': True,
                'verified': bool(written and written['writer'] == 'manual' and written['prompt'] == video.get('prompt')),
                'warnings': [], 'at': written['at'] if written else None}
    return {**written, 'manual': False} if written else None


def _stations(row, board, clips, mats, findings, review) -> list[dict]:
    auto = _auto(row)
    stage = auto.get('stage') or ''
    ready = bool(board) and bool(auto.get('board_ready') or clips)
    failed_source = row.status == 'failed' and not ready
    source = {'key': 'source', 'label': 'Nguồn',
              'status': 'done' if ready else 'attention' if failed_source else 'running' if stage else 'pending',
              'summary': 'Đã có shotlist & danh mục' if ready else (row.error or auto.get('error') or '')[:200] if failed_source else SOURCE_STAGES.get(stage, stage)}
    if not ready:
        return [source] + [{'key': k, 'label': l, 'status': 'pending', 'summary': ''} for k, l in
                           (('shotlist', 'Shotlist & thoại'), ('materials', 'Nhân vật & material'), ('clips', 'Clip & render'), ('review', 'Duyệt & xuất'))]
    blocking = [f for f in findings if f['severity'] == 'blocking' and not f['accepted']]
    shot = {'key': 'shotlist', 'label': 'Shotlist & thoại',
            'status': 'running' if (review or {}).get('status') is None and stage == 'reskin_review' else
                      'attention' if blocking or stage == 'awaiting_review' else 'done',
            'summary': (f'{len(blocking)} lỗi chặn cần xử lý' if blocking else 'Chờ bạn duyệt rồi bấm Tiếp tục' if stage == 'awaiting_review'
                        else f"{sum(c['shot_count'] for c in clips)} shot, {sum(c['line_count'] for c in clips)} câu thoại")}
    slots = [s for m in mats for s in m['slots'] if s.get('used', True)]
    missing = [s for s in slots if not s['image']]
    broken = [s for s in slots if s['status'] in ('failed', 'unknown')]
    busy = [s for s in slots if s['status'] in automation_jobs.ACTIVE]
    mat = {'key': 'materials', 'label': 'Nhân vật & material',
           'status': 'attention' if broken else 'running' if busy else 'pending' if missing else 'done',
           'summary': f"{sum(1 for s in slots if s['image'])}/{len(slots)} sheet" + (f', {len(broken)} lỗi' if broken else '')}
    counts = {}
    for c in clips:
        counts[c['status']] = counts.get(c['status'], 0) + 1
    clip = {'key': 'clips', 'label': 'Clip & render',
            'status': 'attention' if counts.get('failed') or counts.get('held') else 'running' if counts.get('rendering') or counts.get('preparing')
                      else 'done' if clips and counts.get('done') == len(clips) else 'pending',
            'summary': f"{counts.get('done', 0)}/{len(clips)} clip xong" + ''.join(f', {v} {k}' for k, v in counts.items() if k in ('failed', 'held', 'rendering'))}
    needs = [c for c in clips if needs_review(c)]
    films = auto.get('studio_films') or []
    rev = {'key': 'review', 'label': 'Duyệt & xuất',
           'status': 'attention' if needs else 'done' if films and counts.get('done') == len(clips) else 'pending' if not counts.get('done') else 'running',
           'summary': f'{len(needs)} clip cần nghe/xem lại' if needs else f'{len(films)} bản ghép' if films else 'Chưa ghép'}
    return [source, shot, mat, clip, rev]


def _inbox(row, clips, mats, findings) -> list[dict]:
    auto = _auto(row)
    items = []
    if auto.get('stage') == 'awaiting_review':
        items.append({'station': 'shotlist', 'kind': 'gate', 'message': 'Shotlist, danh mục và material đã sẵn sàng — duyệt rồi bấm Tiếp tục.'})
    if row.status == 'failed' and not auto.get('board_ready'):
        items.append({'station': 'source', 'kind': 'error', 'message': (row.error or 'Phân tích dừng.')[:300]})
    for c in clips:
        if any(not f.get('shot') for f in c['blocking']):
            items.append({'station': 'shotlist', 'kind': 'finding', 'clip': c['key'], 'message': f"{c['label']}: {len(c['blocking'])} lỗi đối chiếu nguồn chặn clip."})
        for f in c['blocking']:
            if f.get('shot'):
                items.append({'station': 'shotlist', 'kind': 'finding', 'clip': c['key'],
                              'message': f"{c['label']} shot {str(f['shot']).split(':')[-1]}: " + (f.get('message') or f.get('criterion') or '')[:200]})
        if c['status'] == 'failed':
            items.append({'station': 'clips', 'kind': 'error', 'clip': c['key'], 'message': f"{c['label']}: {c['reason'][:240]}", 'hint': c['hint']})
        for u in c.get('unknown_jobs') or []:
            items.append({'station': 'clips', 'kind': 'unknown', 'clip': c['key'], 'job_id': u['id'],
                          'message': f"{c['label']}: job {u['kind']} chưa rõ trạng thái — {u['error'][:200]}", 'hint': hint(u['error'])})
        if needs_review(c):
            items.append({'station': 'review', 'kind': 'quality', 'clip': c['key'],
                          'message': f"{c['label']}: kiểm tra chất lượng — {', '.join(c['quality']['findings'][:2]) or c['quality']['status']}"})
    for m in mats:
        for s in m['slots']:
            if s['status'] in ('failed', 'unknown'):
                items.append({'station': 'materials', 'kind': 'error', 'node_id': m['node_id'], 'slot': s['slot'],
                              'message': f"{m['name']} — {s['label']}: {s['error'][:200]}", 'hint': s['hint']})
    return items


# ─────────────────────────────── assembly ───────────────────────────────

def _chosen(session_factory, row: VideoAnalysis) -> tuple[dict, list[dict], list[str]]:
    """Each made clip in film order with the take and audio the owner chose."""
    with session_factory() as s:
        project = s.get(AutomationProject, row.automation_project_id)
        board = automation_jobs.project_board(s, project)
        index = Jobs(s.exec(select(AutomationJob).where(AutomationJob.project_id == project.id)).all())
    videos = {n['id'][4:]: n.get('data') or {} for n in board.get('nodes', []) if str(n.get('id', '')).startswith('vid:')}
    chosen, missing = [], []
    for seq in sequences(board):
        key = _key(seq)
        st = clip_state(key, index, {}, set(), videos.get(key))
        if not st['url']:
            missing.append(key)
            continue
        shots = seq['data']['shots']
        chosen.append({'key': key, 'label': seq['data']['sequence'].get('label') or key, 'url': st['url'], 'job_id': st['take_id'],
                       'duration_s': clip_seconds(seq), 'source_start': shots[0]['source_start'], 'source_end': shots[-1]['source_end'],
                       'mute': st['audio_choice'] == 'silent', 'audio_choice': st['audio_choice'],
                       'speech_end_s': (st.get('quality') or {}).get('speech_end_s')})
    return board, chosen, missing


async def assemble(session_factory, row: VideoAnalysis) -> dict:
    """Join each made clip (its chosen take and audio) in film order. Clips not made yet
    are listed, never filled. Cutting to source length never cuts a line: a take whose
    checked speech runs past its source length keeps its whole length."""
    from flowboard.services import production_media
    board, chosen, missing = _chosen(session_factory, row)
    if not chosen:
        raise ValueError('Chưa có clip nào để ghép.')
    policy_name = _settings(row).get('timing_policy', 'full_take')
    kept, unchecked = [], []
    for c in chosen:
        if policy_name == 'source_duration':
            if c['speech_end_s'] is None:
                unchecked.append(c['key'])
            elif float(c['speech_end_s']) > c['duration_s'] - .04:
                c['keep_full'] = True
                kept.append(c['key'])
    result = await production_media.assemble({
        'clips': [{'url': c['url'], 'duration_s': c['duration_s'], 'sequence_key': c['key'], 'job_id': c['job_id'],
                   **({'keep_full': True} if c.get('keep_full') else {}), **({'mute': True} if c['mute'] else {})} for c in chosen],
        'fps': 24, 'aspect_ratio': board.get('aspectRatio', '1:1'), 'resolution': _settings(row).get('resolution', '480p'),
        'timing_policy': policy_name, 'require_quality': False})
    return {'filename': result['filename'], 'duration_s': result['duration_s'], 'at': datetime.now(timezone.utc).isoformat(),
            'clips': [c['key'] for c in chosen], 'missing': missing, 'timing_policy': result['timing_policy'],
            'kept_full': kept, 'unchecked': unchecked}


def _timecode(seconds: float, fps: int = 24) -> str:
    frames = int(round(seconds * fps))
    return f'{frames // (3600 * fps):02d}:{frames // (60 * fps) % 60:02d}:{frames // fps % 60:02d}:{frames % fps:02d}'


def edl(title: str, clips: list[dict], fps: int = 24) -> str:
    """CMX 3600 EDL: one event per clip file, laid end to end."""
    lines = [f'TITLE: {title[:60]}', 'FCM: NON-DROP FRAME', '']
    record = 0.0
    for n, c in enumerate(clips, 1):
        length = float(c['length_s'])
        reel = f'C{n:03d}'
        lines.append(f"{n:03d}  {reel:<8} V     C        {_timecode(0, fps)} {_timecode(length, fps)} {_timecode(record, fps)} {_timecode(record + length, fps)}")
        lines.append(f"* FROM CLIP NAME: {c['file']}")
        lines.append(f"* SOURCE FILM: {_mmss(c['source_start'])}-{_mmss(c['source_end'])} ({c['label']})")
        lines.append('')
        record += length
    return '\n'.join(lines)


async def export(session_factory, row: VideoAnalysis) -> dict:
    """A zip of every made clip (chosen take and audio), an EDL that lays them out, and a
    CSV of what each one is."""
    import csv, io, zipfile
    from pathlib import Path
    from flowboard.config import STORAGE_DIR
    from flowboard.services import production_media as media
    board, chosen, missing = _chosen(session_factory, row)
    if not chosen:
        raise ValueError('Chưa có clip nào để xuất.')
    folder = Path(STORAGE_DIR) / 'production-renders' / 'studio-exports'
    folder.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex + '.zip'
    work = folder / (name + '.parts')
    work.mkdir()
    rows, laid = [], []
    try:
        for n, c in enumerate(chosen, 1):
            raw = work / f"{n:03d}_{c['key']}.raw.mp4"
            await media.fetch(c['url'], raw)
            out = work / f"{n:03d}_{c['key']}.mp4"
            if c['mute']:
                await media.command('ffmpeg', '-v', 'error', '-y', '-i', raw, '-c:v', 'copy', '-an', out)
                raw.unlink()
            else:
                raw.replace(out)
            info = await media.probe(out)
            length = float(info['format']['duration'])
            laid.append({**c, 'file': out.name, 'length_s': length})
            rows.append([n, c['key'], c['label'], out.name, f"{length:.3f}", f"{c['duration_s']:.3f}", _mmss(c['source_start']),
                         _mmss(c['source_end']), c['audio_choice'], c['job_id']])
        table = io.StringIO()
        writer = csv.writer(table)
        writer.writerow(['order', 'clip', 'label', 'file', 'length_s', 'source_length_s', 'source_in', 'source_out', 'audio', 'take_job'])
        writer.writerows(rows)
        with zipfile.ZipFile(folder / name, 'w', zipfile.ZIP_STORED) as z:
            for c in laid:
                z.write(work / c['file'], c['file'])
            z.writestr('film.edl', edl(row.name, laid))
            z.writestr('clips.csv', table.getvalue())
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return {'filename': name, 'at': datetime.now(timezone.utc).isoformat(), 'clips': [c['key'] for c in chosen],
            'missing': missing, 'bytes': (folder / name).stat().st_size}
