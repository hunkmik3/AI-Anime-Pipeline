"""Bound source ASR story context without treating transcription as visual proof."""
from __future__ import annotations

import math


def build_narrative_context(analysis, batch):
    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)
    intervals = [(s.get('start'), s.get('end')) for s in batch
                 if finite(s.get('start')) and finite(s.get('end')) and s['start'] < s['end']]
    source = analysis.get('transcript') or {}
    segments = source.get('segments', []) if isinstance(source, dict) else source if isinstance(source, list) else []
    rows = []
    for i, row in enumerate(segments):
        if not isinstance(row, dict):
            continue
        start, end, text = row.get('start'), row.get('end'), row.get('text')
        if not finite(start) or not finite(end) or start < 0 or end <= start or not isinstance(text, str) or not text.strip():
            continue
        distance = min((max(lo-end, start-hi, 0) for lo, hi in intervals), default=float('inf'))
        if distance <= 8:
            rows.append((distance, i, {'id': f'asr-segment-{i}', 'start': start, 'end': end, 'text': text}))
    selected = sorted(sorted(rows, key=lambda row: (row[0], row[1]))[:36], key=lambda row: row[1])
    return {'origin': 'source_asr_transcript', 'audio_independently_verified': False,
            'scope': 'story_context_only_not_visual_presence_proof',
            'segments': [row[2] for row in selected]}
