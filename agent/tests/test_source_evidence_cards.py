import base64
import io
import json

from PIL import Image

from flowboard.services.video_analyzer.source_evidence_cards import packed_cards


def test_over_limit_keeps_every_frame_and_evidence_binding(tmp_path):
    evidence = []
    for index in range(57):
        path = tmp_path / f'{index}.png'
        Image.new('RGB', (12 + index % 3, 16), (index, 100, 200)).save(path)
        evidence.append({'id': f'e-{index}', 'frame': path.name, 'shot': index + 1})
    parts = packed_cards(evidence, tmp_path, safe_path=lambda root, name: root / name)
    pictures = [part for part in parts if part['type'] == 'imageBase64']
    assert len(pictures) <= 50
    assert all(part['mediaType'] == 'image/jpeg' for part in pictures)  # PNG panels outgrew Avis's request limit
    recovered = []
    for offset in range(1, len(parts), 2):
        cells = json.loads(parts[offset]['text'])['source_evidence_panel']
        image = Image.open(io.BytesIO(base64.b64decode(parts[offset + 1]['data'])))
        for cell in cells:
            item = cell['source_evidence']
            recovered.append(item)
            x, y, w, h = cell['panel_pixel_rect']
            with Image.open(tmp_path / item['frame']) as original:
                crop = image.crop((x, y, x + w, y + h)).convert('RGB')
                assert crop.size == original.size  # full size, never resized
                diff = [abs(a - b) for a, b in zip(crop.tobytes(), original.convert('RGB').tobytes())]
                assert sum(diff) / len(diff) < 6  # JPEG q95: no visible change
    assert recovered == evidence


def test_small_requests_retain_original_transport(tmp_path):
    assert packed_cards([{'frame': 'example.png'}] * 50, tmp_path,
                        safe_path=lambda root, name: root / name) is None


def test_invalid_paths_do_not_consume_image_budget(tmp_path):
    assert packed_cards([{'frame': 'missing.png'}] * 60, tmp_path,
                        safe_path=lambda root, name: None) is None
