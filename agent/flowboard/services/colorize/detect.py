"""Region detection for the Fix editor — panels + objects, plus a grabCut cutout.

No heavy ML: BOXES come from a Gemini vision model (via the Atrium passthrough,
the page hosted by the app's own /thumb URL — same as the bible reader), and
MASKS come from MobileSAM (``flowboard.services.sam``) with grabCut as a
model-free fallback. This keeps the Fix editor torch-free.

  * ``detect_panels`` — each comic/manga PANEL as a box (reading order).
  * ``detect_objects`` — editable parts (garments/accessories/hair/bg) as boxes.
  * ``cutout``         — isolate the object inside a box into a transparent PNG.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from flowboard.services.image import atrium_upscale

logger = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash"
_TIMEOUT_S = 120.0

_PANEL_PROMPT = (
    "Detect every comic/manga PANEL (frame) on this page. Return each panel as a "
    "TIGHT box around its borders, in reading order (top→bottom, right→left for "
    "manga or left→right for western). Include full-bleed and borderless panels. "
    "Output ONLY a JSON array; each item {\"box_2d\": [ymin,xmin,ymax,xmax] "
    "normalized 0-1000}. No prose."
)

_OBJECT_PROMPT = (
    "Detect the editable parts of this image for a photo-editor segment panel. "
    "Return each as a SEPARATE instance with a TIGHT box around only that item's "
    "visible pixels.\n"
    "WHAT TO INCLUDE (and nothing else):\n"
    "- the whole person (one entry)\n"
    "- each WHOLE garment as one entry (blazer, shirt, trousers, dress, skirt, "
    "coat) — do NOT split a garment into lapels/sleeves/legs\n"
    "- each accessory as its OWN instance: a pair = two entries (left earring AND "
    "right earring; left shoe AND right shoe); also bag, belt, glasses, watch, ring\n"
    "- hair (one entry)\n"
    "- background regions: wall, floor, sky\n"
    "DO NOT output skin regions (face, neck, chest, hands) or sub-parts of a garment.\n"
    "Labels: short and descriptive with color/material when clear (e.g. "
    "'black blazer', 'gold hoop earring', 'black hair', 'concrete wall').\n"
    "Output ONLY a JSON array; each item {\"label\": name, \"box_2d\": "
    "[ymin,xmin,ymax,xmax] normalized 0-1000}. No masks, no prose."
)


def is_configured() -> bool:
    import os

    return atrium_upscale.client_creds() is not None and bool(
        os.getenv("PUBLIC_MEDIA_BASE_URL", "").strip()
    )


def _page_url(media_id: str) -> str:
    import os

    base = os.getenv("PUBLIC_MEDIA_BASE_URL", "").strip().rstrip("/")
    return f"{base}/api/media/{media_id}/thumb?w=1536"


async def _vlm_boxes(media_id: str, prompt: str) -> list[dict]:
    """POST one detect prompt + the page (by public /thumb URL) to Gemini via
    Atrium; return the parsed JSON array of ``{label?, box_2d}`` items. [] on any
    failure."""
    creds = atrium_upscale.client_creds()
    if not creds:
        raise RuntimeError("detect: ATRIUM_CLIENT_ID/SECRET not set")
    if not is_configured():
        raise RuntimeError("detect: PUBLIC_MEDIA_BASE_URL not set (page must be hosted by URL)")
    headers = {"x-client-id": creds[0], "x-client-secret": creds[1]}
    body = {
        "model": _MODEL,
        "contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"fileData": {"mimeType": "image/jpeg", "fileUri": _page_url(media_id)}},
        ]}],
        "config": {"responseMimeType": "application/json", "temperature": 0.0},
    }
    url = f"{atrium_upscale.base_url()}/api/partner/llm/generate"
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as c:
        r = await c.post(url, headers=headers, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"detect: http_{r.status_code}: {r.text[:200]}")
    txt = "".join(
        p.get("text", "")
        for cand in r.json().get("candidates", [])
        for p in (cand.get("content") or {}).get("parts", [])
    )
    data = json.loads(txt)
    return data if isinstance(data, list) else []


def _clean_box(box: object) -> Optional[list[int]]:
    if isinstance(box, list) and len(box) == 4:
        try:
            b = [max(0, min(1000, int(x))) for x in box]
        except (TypeError, ValueError):
            return None
        if b[2] - b[0] >= 3 and b[3] - b[1] >= 3:
            return b
    return None


async def detect_panels(media_id: str) -> list[dict]:
    """→ ``[{box}]`` where box is ``[ymin,xmin,ymax,xmax]`` (0-1000), reading
    order. [] on failure."""
    try:
        raw = await _vlm_boxes(media_id, _PANEL_PROMPT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("detect_panels failed: %s", exc)
        return []
    out: list[dict] = []
    for d in raw:
        box = _clean_box((d or {}).get("box_2d") if isinstance(d, dict) else None)
        if box:
            out.append({"box": box})
    return out


async def detect_objects(media_id: str) -> list[dict]:
    """→ ``[{label, box}]`` where box is ``[ymin,xmin,ymax,xmax]`` (0-1000). []
    on failure."""
    try:
        raw = await _vlm_boxes(media_id, _OBJECT_PROMPT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("detect_objects failed: %s", exc)
        return []
    out: list[dict] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        box = _clean_box(d.get("box_2d"))
        label = d.get("label")
        if box and isinstance(label, str) and label.strip():
            out.append({"label": label.strip(), "box": box})
    return out


def cutout(img_bytes: bytes, box_2d: list[int], *, feather: int = 2) -> Optional[bytes]:
    """Isolate the object inside ``box_2d`` ([ymin,xmin,ymax,xmax] 0-1000) into a
    transparent PNG using grabCut. Returns None on failure. CPU-heavy — call via
    ``asyncio.to_thread``. (MobileSAM in ``services.sam`` gives tighter masks; this
    is the model-free fallback.)"""
    try:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        H, W = img.shape[:2]
        ymin, xmin, ymax, xmax = box_2d
        x0 = max(0, int(xmin / 1000 * W)); x1 = min(W, int(xmax / 1000 * W))
        y0 = max(0, int(ymin / 1000 * H)); y1 = min(H, int(ymax / 1000 * H))
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None
        pad_x = max(2, (x1 - x0) // 20); pad_y = max(2, (y1 - y0) // 20)
        cx0, cy0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
        cx1, cy1 = min(W, x1 + pad_x), min(H, y1 + pad_y)
        crop = img[cy0:cy1, cx0:cx1]
        ch, cw = crop.shape[:2]
        mask = np.zeros((ch, cw), np.uint8)
        rect = (x0 - cx0, y0 - cy0, x1 - x0, y1 - y0)
        bgd = np.zeros((1, 65), np.float64); fgd = np.zeros((1, 65), np.float64)
        cv2.grabCut(crop, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
        fg = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
        if feather > 0:
            fg = cv2.GaussianBlur(fg, (0, 0), feather)
        bgra = cv2.cvtColor(crop, cv2.COLOR_BGR2BGRA)
        bgra[:, :, 3] = fg
        bgra = bgra[y0 - cy0:y1 - cy0, x0 - cx0:x1 - cx0]
        ok, buf = cv2.imencode(".png", bgra)
        return buf.tobytes() if ok else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("cutout failed: %s", exc)
        return None
