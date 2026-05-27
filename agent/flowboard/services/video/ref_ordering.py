"""Phase 8.1 — label-driven reference-image ordering.

Seedance 2.0 binds reference semantics positionally: ``@imageN`` in the
prompt text maps to the Nth ``reference_image`` block in the ``content[]``
array (contract §11.2). In Manual mode the user assigns a custom label
(``@image1``, ``@image2``, … or a non-positional name like ``@kenji``) to
each upstream Character / VisualAsset ref node. To make the array order
match those labels, we reorder the collected refs by the digit embedded in
each label before submit.

Resolution rules (locked Phase 8.1 decision Q1 / task (d)):

- A ref whose label contains a digit (``@image2`` → 2) is *labeled*; these
  sort ascending by that digit and take the leading slots.
- A ref with an empty label, or a label with no digit (``@kenji``), is
  *unlabeled*; these keep their original (edge) order and are appended
  after all labeled refs.
- Ties on the same digit keep input (edge) order — the sort is stable.

This module is intentionally pure (no DB, no I/O) so it unit-tests cleanly.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_DIGIT_RE = re.compile(r"\d+")


def resolve_primary_media_id(node_data: Optional[dict[str, Any]]) -> Optional[str]:
    """Phase 8.1.5 — pick the canonical media_id for a ref node (Character /
    VisualAsset) from its persisted ``data``.

    3-tier fallback (locked D6):
      1. ``primary_variant_id`` — the user-chosen primary (a media_id string)
      2. ``mediaId``            — the node's active media (gen sets = variant 1)
      3. ``mediaIds[0]``        — first variant, if neither of the above set

    Returns ``None`` when the node carries no usable media at all. A
    ``primary_variant_id`` that doesn't match any known variant is still
    honored (the user may have pointed it at a custom-uploaded variant whose
    id isn't in a stale ``mediaIds`` snapshot) — we only fall through when it
    is empty / not a string.
    """
    data = node_data or {}
    primary = data.get("primary_variant_id")
    if isinstance(primary, str) and primary:
        return primary
    media_id = data.get("mediaId")
    if isinstance(media_id, str) and media_id:
        return media_id
    media_ids = data.get("mediaIds")
    if isinstance(media_ids, list):
        for m in media_ids:
            if isinstance(m, str) and m:
                return m
    return None


def _label_digit(label: Optional[str]) -> Optional[int]:
    """Extract the first run of digits from a label, or None.

    ``"@image2"`` → 2, ``"@image10"`` → 10, ``"@kenji"`` → None,
    ``""`` / ``None`` → None.
    """
    if not isinstance(label, str):
        return None
    m = _DIGIT_RE.search(label)
    return int(m.group()) if m else None


def order_refs_by_label(
    refs: list[str], labels: list[Optional[str]]
) -> list[str]:
    """Reorder ``refs`` so labeled refs lead (ascending by label digit) and
    unlabeled / no-digit refs follow in original edge order.

    ``labels`` is positionally parallel to ``refs``; a shorter/missing list
    is padded with ``None`` (treated as unlabeled). When every label is
    ``None`` (the common Automation case, or a label-less canvas) the input
    order is returned unchanged — the function is a no-op there.
    """
    n = len(refs)
    # Pad / truncate labels to align with refs so a caller that forgot to
    # keep them in lockstep degrades to "unlabeled" rather than crashing.
    padded = list(labels[:n]) + [None] * max(0, n - len(labels))

    labeled: list[tuple[int, int, str]] = []  # (digit, edge_idx, ref)
    unlabeled: list[tuple[int, str]] = []      # (edge_idx, ref)
    for idx, ref in enumerate(refs):
        digit = _label_digit(padded[idx])
        if digit is None:
            unlabeled.append((idx, ref))
        else:
            labeled.append((digit, idx, ref))

    # Stable across equal digits via the secondary edge-index key.
    labeled.sort(key=lambda t: (t[0], t[1]))
    return [ref for _, _, ref in labeled] + [ref for _, ref in unlabeled]
