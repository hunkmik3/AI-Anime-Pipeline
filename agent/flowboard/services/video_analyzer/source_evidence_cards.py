"""Evidence panels for provider requests with a 50-image limit.

Only transport changes: every source frame keeps its evidence ID, size and
coordinates. Panels are not new observations and cannot be cited as evidence.
Small requests keep their existing one-frame-per-image representation.

Panels are high-quality JPEG, not PNG: the source frames are JPEG already, and
28 PNG panels of 55 frames came to 23 MB, which Avis refused (HTTP 413) on
every refinement batch of a 513-shot film; the same panels as JPEG are 5 MB.
"""
from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path

from PIL import Image

from flowboard.services import avis_text

MAX_IMAGES = 50
JPEG_QUALITY = 95


def packed_cards(evidence: list[dict], work_dir: Path, *, safe_path):
    valid = [(item, path) for item in evidence
             if (path := safe_path(work_dir, item['frame']))]
    if len(valid) <= MAX_IMAGES:
        return None
    group_size = math.ceil(len(valid) / MAX_IMAGES)
    output = [avis_text.text_part(
        'SOURCE EVIDENCE TRANSPORT: the following images are full-size panels '
        'containing separate source frames. Each cell is identified by its '
        'original evidence ID and pixel rectangle in the preceding JSON. '
        'Cells are separate timestamps/views, never a single combined scene. '
        'Cite original evidence IDs only. Source crop requests still use '
        'normalized coordinates of the ORIGINAL source frame, not this panel.'
    )]
    for start in range(0, len(valid), group_size):
        images, cells = [], []
        left = 0
        for item, path in valid[start:start + group_size]:
            with Image.open(path) as original:
                frame = original.convert('RGB')
            images.append(frame)
            cells.append({'source_evidence': item,
                          'panel_pixel_rect': [left, 0, frame.width, frame.height]})
            left += frame.width + 8
        panel = Image.new('RGB', (left - 8, max(im.height for im in images)), 'black')
        for frame, cell in zip(images, cells):
            panel.paste(frame, tuple(cell['panel_pixel_rect'][:2]))
        buffer = io.BytesIO()
        panel.save(buffer, format='JPEG', quality=JPEG_QUALITY)
        output += [avis_text.text_part(json.dumps({'source_evidence_panel': cells}, ensure_ascii=False)),
                   {'type': 'imageBase64', 'mediaType': 'image/jpeg',
                    'data': base64.b64encode(buffer.getvalue()).decode('ascii')}]
    return output
