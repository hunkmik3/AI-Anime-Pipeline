"""Approved source-motion reskin recipe, shared by upload and board production.

Source observations remain immutable. A motion receipt is bound to source bytes,
exact cuts and the reviewed shotlist; target designs are bound separately.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path
import uuid
from functools import lru_cache

from flowboard.config import STORAGE_DIR
from flowboard.services.production_manifest import digest

VERSION = 'source-motion-reskin-v1'
MAX_REFERENCE_SECONDS = 15.0
# The white-model render for long films: depth at a 448 px long side, every second frame
# of a shot (interpolated inside the shot), 24 fps (the KYC video floor is 23.8 fps).
# About 3x faster per clip than the standard render; boards without it keep their exact
# standard payload, so nothing already made is rendered again.
FAST_RENDER = {'depth_long_side': 448, 'fps': 24, 'depth_stride': 2}
POLICY = '''SOURCE MOTION / TARGET DESIGN CONTROL
@video1 is a silent depth-shaded motion reference of the selected original shots.
Follow its cut order, camera motion, framing, actions, eyelines, prop custody and
crowd movement. It is not the desired surface appearance and not a reconstructed
3D scene. Use the attached approved designs for identity, wardrobe, environment,
objects and target visual style. A crowd sheet contains different people, never
duplicate every face or turn its cells into extra principals. Preserve recurring
extras and their positions through the cuts, showing only the visible subset.
The single wide staging image guides relative positions, not shot framing: never
freeze all shots into that pose or turn it into a split-screen storyboard.
The output aspect ratio is selected independently of the source and staging image.
For a changed ratio, extend the missing edges plausibly, preserving the principal
action, relative positions and subject scale. Do not stretch people or crop away
essential action merely to fill the output frame. Source composition remains the guide.
The supplied dialogue is the only speech; the motion reference supplies no audio.
'''.strip()


def enabled(board):
    return board.get('motionReference') == 'white_model'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


@lru_cache(maxsize=16)
def _source_hash(path, stat_key):
    return sha256(path)


def source_fingerprint(path):
    """Read a large upload once per file version, not once for every 15s clip."""
    path=Path(path);stat=path.stat()
    return _source_hash(str(path.resolve()),(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))


def source_path(video_id):
    # Resolve server-owned uploads only, never arbitrary paths from a client.
    folder = Path(STORAGE_DIR) / 'video_analysis' / str(uuid.UUID(str(video_id)))
    paths = [p for p in folder.glob('source.*') if p.suffix.lower() in {'.mp4', '.mov', '.mkv', '.webm', '.m4v'}]
    if len(paths) != 1:
        raise ValueError('Không tìm thấy video gốc đã upload; cần khôi phục file nguồn trước khi chạy reskin.')
    return paths[0]


def source_contract(board):
    return {'source_video_id': board.get('sourceVideoId'), 'source_sha256': board.get('sourceMediaSha256'),
            'verification': board.get('sourceVerification'),
            'assets': deepcopy(board.get('productionAssets',[])),
            'shots': [deepcopy(n['data']['shots']) for n in board.get('nodes', [])
                      if n.get('data', {}).get('kind') == 'sequence']}


def selected_shots(board, key):
    return next(n['data']['shots'] for n in board['nodes'] if n['id'] == 'seq:' + key)


def motion_payload(board, package):
    shots = selected_shots(board, package['sequence_key'])
    ranges = [(float(s['source_start']), float(s['source_end'])) for s in shots]
    if not ranges or any(not math.isfinite(a + b) or a < 0 or b <= a for a, b in ranges):
        raise ValueError('Reskin cần mốc đầu/cuối video gốc hợp lệ cho từng shot.')
    if any(abs(a - ranges[i-1][1]) > .05 for i, (a, _) in enumerate(ranges) if i):
        raise ValueError('Các shot trong clip không liên tiếp trên nguồn; chia clip tại khoảng nhảy thời gian.')
    start, end = ranges[0][0], ranges[-1][1]
    if end - start > MAX_REFERENCE_SECONDS + .001:
        raise ValueError('Clip reskin dài hơn 15 giây; chia tại cut nguồn trước khi chạy.')
    if any(abs((b-a)-float(s['duration_s'])) > .05 for (a,b),s in zip(ranges, shots)):
        raise ValueError('Thời lượng shot đã khác video gốc; không thể gắn motion reference.')
    sha = board.get('sourceMediaSha256')
    if not sha:
        raise ValueError('Thiếu dấu vân tay video nguồn. Nhập lại qua pipeline Reskin.')
    return {'version': VERSION, 'source_video_id': board['sourceVideoId'], 'source_sha256': sha,
            'start': start, 'end': end, 'cuts': [a-start for a, _ in ranges[1:]],
            'shot_ids': [s.get('source_shots') or [s.get('source_shot')] for s in shots],
            'source_review_digest': digest([r for r in (board.get('reskinSourceReview') or {}).get('receipts',[])
                                           if r.get('clip') == package['sequence_key']]),
            **({'fps': FAST_RENDER['fps'], 'render': FAST_RENDER} if board.get('motionProfile') == 'fast' else {'fps': 30})}


def split_readiness(board, packages):
    """(issues that stop every clip, {clip key: issues that stop only that clip}).

    The review's input digest covers the whole board, so an edit anywhere asks for a
    re-review (unchanged intervals reuse their receipts). Inside a current review each
    clip stands on its own receipt: a blocking finding the owner has not accepted holds
    that clip only; warnings never hold one (see reskin_review_policy)."""
    if not enabled(board):
        return [], {}
    shared, held = [], {}
    review = board.get('reskinSourceReview') or {}
    if review.get('input_digest') != digest(source_contract(board)):
        shared.append('Shotlist reskin chưa đối chiếu đủ với hình/âm thanh nguồn, hoặc đã thay đổi sau đối chiếu.')
    if not board.get('kyc'):
        shared.append('Reskin cần bật KYC để gắn đồng thời video chuyển động và các sheet đã chọn.')
    from flowboard.services import reskin_source_review, reskin_review_policy as policy
    for package in packages:
        key = package['sequence_key']
        receipt = next((r for r in review.get('receipts',[]) if r.get('clip') == key),None)
        try:
            if not receipt or receipt.get('source_sha256') != board.get('sourceMediaSha256'):
                raise ValueError('Thiếu bằng chứng đúng phiên bản nguồn.')
            if receipt.get('validation_error'):
                raise ValueError('Lượt đối chiếu nguồn trả về thiếu cấu trúc: ' + str(receipt['validation_error'])[:200])
            rows = [{**s,'id':f"{key}:{i+1}"} for i,s in enumerate(selected_shots(board,key))]
            found = reskin_source_review.validate(receipt.get('observations'),rows,max(0,rows[0]['source_start']-1))
            stops = policy.blocking(found, receipt.get('input_digest',''), board)
            if stops:
                raise ValueError(f'Còn {len(stops)} phát hiện nguồn cần xử lý: ' + '; '.join(policy.describe(f) for f in stops[:3]))
        except (ValueError,KeyError,TypeError,StopIteration) as exc:
            held.setdefault(key, []).append(str(exc))
    for package in packages:
        try:
            motion_payload(board, package)
        except (ValueError, KeyError, TypeError, StopIteration) as exc:
            held.setdefault(package['sequence_key'], []).append(str(exc))
    return shared, held


def readiness(board, packages):
    """Every issue for these packages, flat: what any one of them still needs."""
    shared, held = split_readiness(board, packages)
    return shared + [f'{key}: {issue}' for key, issues in held.items() for issue in issues]


def staging_payload(board, package, references):
    """One continuous wide scene per clip; never generate per-shot stills."""
    facts = [{k: s.get(k) for k in ('id','framing','staging','scene_layout','action','visible_asset_ids','observed','transitions')}
             for s in package['shots']]
    import json
    prompt = ('Create ONE continuous 21:9 wide cinematic staging scene, no panels, collage, text, labels or turnarounds. '
              'Use only the supplied approved cast, environment, props and crowd designs in their chosen style. '
              'Show a representative moment at the beginning of this segment, relative positions, eyelines, '
              'motivated action and relevant held props. Do not combine sequential poses into duplicated people. '
              'Keep architecture scale and openings; background extras are distinct people with stable attire. '
              'Do not bring later arrivals into the opening. Design bindings and source facts follow as data:\n' +
              json.dumps({'style': board['style'], 'references': references, 'shots': facts}, ensure_ascii=False))
    return {'prompt': prompt, 'reference_urls': list(dict.fromkeys(r['url'] for r in references)),
            'image_model': board.get('imageModel', 'dola-seedream-5-0-pro'), 'image_size': board.get('imageSize','2K'),
            'aspect_ratio': '21:9'}


def enforce_prompt(prompt):
    from flowboard.services.cinematic_prompt import enforce_audio_policy
    prompt = enforce_audio_policy(prompt)
    if POLICY not in prompt:
        prompt = prompt.replace('## REFERENCE CONTROL', '## REFERENCE CONTROL\n' + POLICY + '\n', 1)
    return prompt


def validate_motion(session, project, body):
    from flowboard.db.models import AutomationJob
    from flowboard.services import automation_jobs, shot_package
    board = automation_jobs.project_board(session, project)
    if not enabled(board):
        if body.motion_media_id or body.motion_job_id:
            raise ValueError('Motion reference chỉ dùng cho board Reskin.')
        return
    package = shot_package.build(board, str(project.id), body.sequence_key)
    problems = readiness(board, [package])
    if problems:
        raise ValueError('; '.join(problems))
    if not body.prompt_contract or POLICY not in body.prompt:
        raise ValueError('Viết lại prompt bằng pipeline Reskin trước khi gen.')
    job = session.get(AutomationJob, uuid.UUID(body.motion_job_id)) if body.motion_job_id else None
    if (not job or job.project_id != project.id or job.kind != 'motion_reference' or job.status != 'succeeded'
            or job.payload != motion_payload(board, package) or job.result.get('media_id') != body.motion_media_id
            or body.prompt_contract.sequence.get('motion_receipt') != job.result):
        raise ValueError('Video chuyển động không khớp phiên bản nguồn/shotlist đã duyệt.')
