"""Manga-colorizer worker handlers (read-first, color-locked).

Four request types, all STATELESS — the colorize sequence (a Shot ``kind=
"colorize"``) holds the chapter state (pages/bible/sheets/outputs/variants) in
``Shot.workflow_metadata`` on the FRONTEND, which orchestrates these jobs and
persists their results. So each handler takes its inputs in ``params`` and
returns its outputs; it never reads/writes the DB row (no cross-page write race).

  colorize_build_bible  : read the chapter pages → a colour "bible" (JSON).
  colorize_build_sheets : per main-cast outfit → a colorized character model-sheet.
  colorize_page         : COLOR-LOCK prompt from the bible + Seedream → coloured page.
  colorize_fix_region   : re-colorize / free-text-edit ONE region, composited back.

Engine: ``flowboard.services.image.seedream`` (DanceSee B2B / Avis). Detection for
the Fix editor lives in the routes layer (``routes/colorize.py``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Optional, Sequence

from flowboard.services import media as media_service
from flowboard.services.colorize import bible as bible_mod, prompt as prompt_mod, reader
from flowboard.services.image import seedream as engine

logger = logging.getLogger(__name__)


# ── small helpers ─────────────────────────────────────────────────────────────

def _colorize_page_key(i: int) -> str:
    """Ordinal page name used as the bible key for the i-th uploaded page."""
    return f"page_{i + 1:03d}"


def _load_media_bytes(mid: object) -> Optional[bytes]:
    p = media_service.cached_path(mid) if isinstance(mid, str) and mid else None
    if p is None:
        return None
    try:
        return p.read_bytes() or None
    except OSError:
        return None


def _ingest_png(png: bytes) -> str:
    cid = str(uuid.uuid4())
    media_service.ingest_inline_bytes(cid, png, kind="image", mime="image/png")
    return cid


def _downscale_jpeg(b: Optional[bytes], edge: int = 2560) -> tuple[Optional[bytes], Optional[tuple]]:
    """Downscale to ``edge`` long side as JPEG q92. Returns (bytes, (w,h))."""
    if not b:
        return None, None
    try:
        import io as _io

        from PIL import Image
        im = Image.open(_io.BytesIO(b)).convert("RGB")
        w, h = im.size
        m = max(w, h)
        if m > edge:
            s = edge / m
            im = im.resize((max(1, round(w * s)), max(1, round(h * s))))
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue(), im.size
    except Exception:  # noqa: BLE001
        return b, None


def _enhance_colors(img_bytes: bytes, sat: float, contrast: float) -> bytes:
    """Deterministic vibrance: scale LAB a*/b* chroma by ``sat`` (hue-preserving)
    and add gentle L-contrast by ``contrast``. 1.0 = no-op. Best-effort."""
    if (abs(sat - 1.0) < 1e-3) and (abs(contrast - 1.0) < 1e-3):
        return img_bytes
    try:
        import cv2
        import numpy as np

        arr = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return img_bytes
        lab = cv2.cvtColor(arr, cv2.COLOR_BGR2LAB).astype(np.float32)
        if abs(sat - 1.0) >= 1e-3:
            lab[:, :, 1] = np.clip((lab[:, :, 1] - 128.0) * sat + 128.0, 0, 255)
            lab[:, :, 2] = np.clip((lab[:, :, 2] - 128.0) * sat + 128.0, 0, 255)
        if abs(contrast - 1.0) >= 1e-3:
            lab[:, :, 0] = np.clip((lab[:, :, 0] - 128.0) * contrast + 128.0, 0, 255)
        out = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2BGR)
        ok, buf = cv2.imencode(".png", out)
        return buf.tobytes() if ok else img_bytes
    except Exception:  # noqa: BLE001
        return img_bytes


_AUTO_COLORREF_CACHE: dict[tuple, Optional[str]] = {}


def _auto_color_ref(page_ids: list[str]) -> Optional[str]:
    """Pick the most colourful page (a colour cover/splash) as an image style
    reference — real colours anchor the palette far better than a text prompt.
    Cached per page-set; None when every page is essentially B&W."""
    key = tuple(page_ids)
    if key in _AUTO_COLORREF_CACHE:
        return _AUTO_COLORREF_CACHE[key]
    best_mid: Optional[str] = None
    best_sat = 0.0
    try:
        import cv2
        import numpy as np

        for mid in page_ids:
            b = _load_media_bytes(mid)
            if not b:
                continue
            arr = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
            if arr is None:
                continue
            h, w = arr.shape[:2]
            sc = 200.0 / max(h, w)
            small = cv2.resize(arr, (max(1, int(w * sc)), max(1, int(h * sc))))
            sat = float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1].mean())
            if sat > best_sat:
                best_sat, best_mid = sat, mid
    except Exception:  # noqa: BLE001
        best_mid = None
    ref = best_mid if best_sat >= 25.0 else None
    _AUTO_COLORREF_CACHE[key] = ref
    return ref


def _sheet_source_index(bible, outfit_id: str) -> Optional[int]:
    """Page index to build an outfit's sheet from — a page where that (char,
    outfit) appears, preferring one where the character is ALONE."""
    import re

    o = bible.outfits.get(outfit_id)
    if not o:
        return None
    order = list(bible.meta.page_order) or list(bible.pages.keys())
    cands: list[tuple[int, int, str]] = []
    for pos, pg in enumerate(order):
        page = bible.pages.get(pg)
        if not page:
            continue
        if any(pr.char == o.char and pr.outfit == outfit_id for pr in page.present):
            cands.append((len({pr.char for pr in page.present}), pos, pg))
    if not cands:
        return None
    cands.sort(key=lambda t: (t[0], t[1]))
    pg = cands[0][2]
    m = re.search(r"(\d+)", pg)
    return (int(m.group(1)) - 1) if m else cands[0][1]


def _model() -> str:
    return os.getenv("COLORIZE_MODEL", "").strip() or os.getenv("SEEDREAM_MODEL", "").strip() or engine.default_model()


def _parse_bible(raw: object):
    return bible_mod.parse_bible(raw)


# ── PASS 1 — bible ────────────────────────────────────────────────────────────

async def handle_build_bible(params: dict) -> tuple[dict, Optional[str]]:
    """params: page_media_ids[], chapter_name?, style_ref_media_id?
    → {bible, summary}."""
    page_ids = [m for m in (params.get("page_media_ids") or []) if isinstance(m, str) and m]
    if not page_ids:
        return {}, "no_pages"
    if not reader.is_configured():
        return {}, "reader_not_configured (need ATRIUM creds + PUBLIC_MEDIA_BASE_URL)"
    name = (params.get("chapter_name") or "").strip()
    style_id = params.get("style_ref_media_id") or None
    pages = [(_colorize_page_key(i), mid) for i, mid in enumerate(page_ids)]
    try:
        bible = await reader.build_bible(pages, chapter=name, style_ref_id=style_id)
    except Exception as exc:  # noqa: BLE001
        return {}, f"bible_failed: {type(exc).__name__}: {exc}"[:200]
    return {"bible": bible.model_dump(), "summary": bible.summary()}, None


# ── PASS 1.5 — character sheets ───────────────────────────────────────────────

async def handle_build_sheets(params: dict) -> tuple[dict, Optional[str]]:
    """params: page_media_ids[], bible{}, outfit_ids?[] → {sheets:{oid:mid}, ...}."""
    if not engine.is_configured():
        return {}, "seedream_not_configured (DANCESEE_API_KEY / AVIS_API_KEY)"
    page_ids = [m for m in (params.get("page_media_ids") or []) if isinstance(m, str) and m]
    raw_bible = params.get("bible")
    if raw_bible is None:
        return {}, "no_bible"
    only = params.get("outfit_ids") if isinstance(params.get("outfit_ids"), list) else None
    try:
        bible = _parse_bible(raw_bible)
    except Exception as exc:  # noqa: BLE001
        return {}, f"bad_bible: {exc}"[:200]

    cover_ref = await asyncio.to_thread(_auto_color_ref, page_ids) if os.getenv("COLORIZE_AUTO_COLORREF", "1") != "0" else None
    cover_bytes, _ = await asyncio.to_thread(_downscale_jpeg, _load_media_bytes(cover_ref) if cover_ref else None)
    model = _model()

    outfit_ids = only or list(bible.outfits.keys())
    if only is None:
        # MAIN-CAST only: keep characters appearing on >= COLORIZE_SHEET_MIN_RATIO
        # of pages (default 25%), floor 3.
        npages = max(1, len(bible.pages))
        min_pages = max(3, int(npages * float(os.getenv("COLORIZE_SHEET_MIN_RATIO", "0.25"))))
        appear: dict[str, int] = {}
        for pg in bible.pages.values():
            for pr in pg.present:
                appear[pr.char] = appear.get(pr.char, 0) + 1
        outfit_ids = [
            oid for oid in outfit_ids
            if (o := bible.outfits.get(oid)) is not None and appear.get(o.char, 0) >= min_pages
        ]
    if not outfit_ids:
        return {}, "no_outfits: no character appears on enough pages for a sheet"

    sheet_size = engine.size_for_dims(2560, 1536)
    sem = asyncio.Semaphore(int(os.getenv("COLORIZE_SHEET_CONCURRENCY", "3")))

    async def _one(oid: str) -> tuple[str, Optional[str], Optional[str]]:
        async with sem:
            o = bible.outfits.get(oid)
            char_id = o.char if o else ""
            ref_colored = bool(cover_bytes)
            if cover_bytes:
                ref_ds: Optional[bytes] = cover_bytes
            else:
                idx = _sheet_source_index(bible, oid)
                if idx is None or not (0 <= idx < len(page_ids)):
                    return oid, None, "no source page for outfit"
                page_bytes, dims = await asyncio.to_thread(_downscale_jpeg, _load_media_bytes(page_ids[idx]))
                if page_bytes is None:
                    return oid, None, "source page not cached"
                page_text = prompt_mod.build_prompt(bible, _colorize_page_key(idx))
                psize = engine.size_for_dims(*dims) if dims else None
                try:
                    colored = await engine.generate_image_variants(
                        page_text, [page_bytes], image_model=model, variant_count=1, size_override=psize)
                except Exception as exc:  # noqa: BLE001
                    return oid, None, str(exc)[:160]
                if not colored:
                    return oid, None, "no image from ref colorize"
                ref_ds, _ = await asyncio.to_thread(_downscale_jpeg, colored[0])
            sheet_text = prompt_mod.build_sheet_prompt(bible, char_id, oid, ref_is_colored=ref_colored)
            try:
                sheet = await engine.generate_image_variants(
                    sheet_text, [ref_ds] if ref_ds else [], image_model=model,
                    variant_count=1, size_override=sheet_size)
            except Exception as exc:  # noqa: BLE001
                return oid, None, str(exc)[:160]
            if not sheet:
                return oid, None, "no image from turnaround"
            mid = await asyncio.to_thread(_ingest_png, sheet[0])
            return oid, mid, None

    results = await asyncio.gather(*[_one(o) for o in outfit_ids])
    built: dict[str, str] = {oid: mid for oid, mid, _err in results if mid}
    failures = [f"{oid}: {err}" for oid, mid, err in results if not mid and err]
    payload = {"sheets": built, "count": len(built), "failures": failures}
    if not built:
        return payload, f"sheets_failed: {failures[0] if failures else 'all outfits failed'}"[:200]
    return payload, None


# ── PASS 2 — colorize one page ────────────────────────────────────────────────

def _page_ref_ids(bible, page_key: str, sheets: dict) -> tuple[list[str], bool]:
    """Character-sheet media ids for the (char,outfit) present on this page."""
    ref_ids: list[str] = []
    page_obj = bible.pages.get(page_key)
    if page_obj:
        seen: set[str] = set()
        for pr in page_obj.present:
            sid = sheets.get(pr.outfit) if pr.outfit else None
            if sid and sid not in seen:
                seen.add(sid)
                ref_ids.append(sid)
    return ref_ids[:3], bool(ref_ids)


async def handle_colorize_page(params: dict) -> tuple[dict, Optional[str]]:
    """params: page_media_ids[], page_index, bible{}, sheets?{}, style_ref_media_id?,
    variant_count? → {output_media_id, variant_media_ids, page_index, page_media_id}."""
    if not engine.is_configured():
        return {}, "seedream_not_configured (DANCESEE_API_KEY / AVIS_API_KEY)"
    page_ids = [m for m in (params.get("page_media_ids") or []) if isinstance(m, str) and m]
    raw_bible = params.get("bible")
    try:
        page_index = int(params.get("page_index"))
    except (TypeError, ValueError):
        return {}, "bad_page_index"
    if raw_bible is None:
        return {}, "no_bible"
    if not (0 <= page_index < len(page_ids)):
        return {}, "bad_page_index"
    try:
        bible = _parse_bible(raw_bible)
    except Exception as exc:  # noqa: BLE001
        return {}, f"bad_bible: {exc}"[:200]

    sheets = params.get("sheets") if isinstance(params.get("sheets"), dict) else {}
    style_id = params.get("style_ref_media_id") or None
    page_key = _colorize_page_key(page_index)

    ref_ids, used_sheets = _page_ref_ids(bible, page_key, sheets)
    if not ref_ids:
        if not style_id and os.getenv("COLORIZE_AUTO_COLORREF", "1") != "0":
            style_id = await asyncio.to_thread(_auto_color_ref, page_ids)
        if style_id:
            ref_ids = [style_id]
    ref_ids = ref_ids[:3]
    page_mid = page_ids[page_index]

    def _prep() -> tuple[Optional[bytes], list, Optional[tuple]]:
        pb, pdims = _downscale_jpeg(_load_media_bytes(page_mid))
        refs: list = []
        for rid in ref_ids:
            rb, _ = _downscale_jpeg(_load_media_bytes(rid))
            if rb:
                refs.append(rb)
        return pb, refs, pdims

    page_bytes, ref_bytes, dims = await asyncio.to_thread(_prep)
    if page_bytes is None:
        return {}, "page_not_cached"

    text = prompt_mod.build_prompt(bible, page_key, sheet_refs=used_sheets)
    size = engine.size_for_dims(*dims) if dims else None
    images = [page_bytes] + ref_bytes
    try:
        n_variants = max(1, min(int(params.get("variant_count", 1)), 4))
    except (TypeError, ValueError):
        n_variants = 1
    try:
        outs = await engine.generate_image_variants(
            text, images, image_model=_model(), variant_count=n_variants, size_override=size)
    except Exception as exc:  # noqa: BLE001
        return {}, f"colorize_failed: {type(exc).__name__}: {exc}"[:200]
    if not outs:
        return {}, "no_image_generated"

    sat = float(os.getenv("COLORIZE_SATURATION", "1.0"))
    contrast = float(os.getenv("COLORIZE_CONTRAST", "1.0"))
    if abs(sat - 1.0) > 1e-3 or abs(contrast - 1.0) > 1e-3:
        outs = [await asyncio.to_thread(_enhance_colors, o, sat, contrast) for o in outs]

    out_ids = [m for m in await asyncio.to_thread(lambda: [_ingest_png(o) for o in outs]) if m]
    return {
        "output_media_id": out_ids[0] if out_ids else None,
        "variant_media_ids": out_ids,
        "page_index": page_index,
        "page_media_id": page_mid,
    }, None


# ── Fix ONE region ────────────────────────────────────────────────────────────

async def handle_fix_region(params: dict) -> tuple[dict, Optional[str]]:
    """params: page_media_ids[], page_index, bible{}, current_output_media_id,
    sheets?{}, box[4] (ymin,xmin,ymax,xmax 0-1000), use_mask(bool), prompt?(free-text
    edit; empty = plain re-colorize) → {output_media_id, page_index, page_media_id}."""
    import cv2
    import numpy as np

    from flowboard.services import sam

    if not engine.is_configured():
        return {}, "seedream_not_configured"
    page_ids = [m for m in (params.get("page_media_ids") or []) if isinstance(m, str) and m]
    raw_bible = params.get("bible")
    try:
        page_index = int(params.get("page_index"))
        box = [int(v) for v in params.get("box")]
        assert len(box) == 4
    except (TypeError, ValueError, AssertionError):
        return {}, "bad_params (need page_index, box[4])"
    if raw_bible is None:
        return {}, "no_bible"
    if not (0 <= page_index < len(page_ids)):
        return {}, "bad_page_index"
    cur_out = params.get("current_output_media_id")
    if not isinstance(cur_out, str) or not cur_out:
        return {}, "page_not_colorized_yet"
    use_mask = bool(params.get("use_mask"))
    edit_prompt = (params.get("prompt") or "").strip()
    sheets = params.get("sheets") if isinstance(params.get("sheets"), dict) else {}
    try:
        bible = _parse_bible(raw_bible)
    except Exception as exc:  # noqa: BLE001
        return {}, f"bad_bible: {exc}"[:150]

    page_mid = page_ids[page_index]
    page_key = _colorize_page_key(page_index)
    ref_ids, used_sheets = _page_ref_ids(bible, page_key, sheets)

    bw_bytes = _load_media_bytes(page_mid)
    out_bytes = _load_media_bytes(cur_out)
    if not bw_bytes or not out_bytes:
        return {}, "media_not_cached"
    crop_src_bytes = out_bytes if edit_prompt else bw_bytes

    # Crop a PADDED region around the box, not the tight box: to remove/replace an
    # object the model must see the surrounding art to rebuild behind it — a tight
    # crop has no context and fills with faint smudges + leaves a visible seam. The
    # padding is context only; we composite the box back feathered.
    PAD = float(os.getenv("COLORIZE_FIX_PAD", "0.4") or "0.4")

    def _pbox(w: int, h: int) -> tuple[int, int, int, int]:
        bx0 = box[1] / 1000 * w; bx1 = box[3] / 1000 * w
        by0 = box[0] / 1000 * h; by1 = box[2] / 1000 * h
        mw = (bx1 - bx0) * PAD; mh = (by1 - by0) * PAD
        return (max(0, int(bx0 - mw)), max(0, int(by0 - mh)),
                min(w, int(bx1 + mw)), min(h, int(by1 + mh)))

    def _prep() -> Optional[tuple]:
        src = cv2.imdecode(np.frombuffer(crop_src_bytes, np.uint8), cv2.IMREAD_COLOR)
        out = cv2.imdecode(np.frombuffer(out_bytes, np.uint8), cv2.IMREAD_COLOR)
        if src is None or out is None:
            return None
        hb, wb = src.shape[:2]
        px0, py0, px1, py1 = _pbox(wb, hb)
        if px1 - px0 < 8 or py1 - py0 < 8:
            return None
        crop = src[py0:py1, px0:px1]
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
        cb, dims = _downscale_jpeg(buf.tobytes())
        refs = []
        for rid in ref_ids:
            rb, _ = _downscale_jpeg(_load_media_bytes(rid))
            if rb:
                refs.append(rb)
        return out, cb, dims, refs

    prepped = await asyncio.to_thread(_prep)
    if prepped is None:
        return {}, "bad_crop"
    out_img, crop_bytes, dims, ref_bytes = prepped

    if edit_prompt:
        text = (
            f"This is a crop from an anime page. Edit it as instructed: {edit_prompt}. "
            "When removing or replacing something, cleanly REBUILD the plausible "
            "background behind it (wall, floor, furniture, sky, whatever fits) using the "
            "surrounding art as the reference, leaving NO trace — no leftover outline, "
            "ghost, smudge, blur, discoloured patch, faint rectangle or empty area. Keep "
            "everything not mentioned unchanged (composition, framing, line art, character "
            "identity). Clean 2D anime cel colouring, flat solid colours; match the "
            "surrounding palette, texture and lighting seamlessly. No text, no bubbles."
        )
        gen_images = [crop_bytes]
    else:
        text = prompt_mod.build_prompt(bible, page_key, sheet_refs=used_sheets)
        gen_images = [crop_bytes] + ref_bytes
    size = engine.size_for_dims(*dims) if dims else None
    try:
        outs = await engine.generate_image_variants(
            text, gen_images, image_model=_model(), variant_count=1, size_override=size)
    except Exception as exc:  # noqa: BLE001
        return {}, f"fix_failed: {type(exc).__name__}: {exc}"[:180]
    if not outs:
        return {}, "no_image_generated"

    def _composite() -> Optional[bytes]:
        result = out_img.copy()
        ho, wo = result.shape[:2]
        # The model edited the PADDED region; composite it back over that same region.
        px0, py0, px1, py1 = _pbox(wo, ho)
        tw, th = px1 - px0, py1 - py0
        if tw < 4 or th < 4:
            return None
        colored = cv2.imdecode(np.frombuffer(outs[0], np.uint8), cv2.IMREAD_COLOR)
        colored = cv2.resize(colored, (tw, th), interpolation=cv2.INTER_CUBIC)
        alpha = np.zeros((th, tw), np.float32)
        if use_mask:
            # Only the object inside the box changes; SAM masks it, padding stays put.
            bx0 = max(0, int(box[1] / 1000 * wo)) - px0
            by0 = max(0, int(box[0] / 1000 * ho)) - py0
            bx1 = min(wo, int(box[3] / 1000 * wo)) - px0
            by1 = min(ho, int(box[2] / 1000 * ho)) - py0
            cut = sam.cutout_box(crop_src_bytes, box,
                                 media_id=(cur_out if edit_prompt else page_mid)) if sam.available() else None
            m = None
            if cut:
                a = cv2.imdecode(np.frombuffer(cut, np.uint8), cv2.IMREAD_UNCHANGED)
                if a is not None and a.ndim == 3 and a.shape[2] == 4:
                    m = cv2.resize(a[:, :, 3], (max(1, bx1 - bx0), max(1, by1 - by0)),
                                   interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
            if m is None:
                m = np.ones((max(1, by1 - by0), max(1, bx1 - bx0)), np.float32)
            alpha[by0:by1, bx0:bx1] = m[: by1 - by0, : bx1 - bx0]
            alpha = cv2.GaussianBlur(alpha, (0, 0), 2)
        else:
            # Whole region changes; feather a soft ring inside the padding so the edit
            # melts into the untouched surround — no hard rectangle. Centre → 1, ramp
            # to 0 across the outer margin.
            fx = max(3, int(tw * 0.16)); fy = max(3, int(th * 0.16))
            yy = np.minimum(np.arange(th), th - 1 - np.arange(th)).astype(np.float32)[:, None] / fy
            xx = np.minimum(np.arange(tw), tw - 1 - np.arange(tw)).astype(np.float32)[None, :] / fx
            alpha = np.clip(np.minimum(yy, xx), 0.0, 1.0)
        roi = result[py0:py1, px0:px1].astype(np.float32)
        blended = colored.astype(np.float32) * alpha[..., None] + roi * (1 - alpha[..., None])
        result[py0:py1, px0:px1] = blended.astype(np.uint8)
        ok, buf = cv2.imencode(".png", result)
        return buf.tobytes() if ok else None

    new_png = await asyncio.to_thread(_composite)
    if not new_png:
        return {}, "composite_failed"
    new_mid = await asyncio.to_thread(_ingest_png, new_png)
    return {"output_media_id": new_mid, "page_index": page_index, "page_media_id": page_mid}, None
