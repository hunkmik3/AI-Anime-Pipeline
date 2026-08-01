"""Flow Studio image engines — three interchangeable HTTP providers.

Brought over from the manga_extract repo (`services/comic/{gemini,atrium,ark}_api.py`)
essentially unchanged: each is a self-contained httpx client with no shared state and
no dependency on the comic pipeline or the Google Flow browser bridge.

  gemini  — Gemini image API direct; reference/source images sent inline as bytes,
            so it works with nothing but a key. Quota-based, not billed per image.
  atrium  — Atrium gateway passthrough, speaks Gemini model ids. Input images must
            be PUBLIC urls, so refs/edits need R2 or a tunnel; plain text→image
            needs neither.
  ark     — BytePlus Ark direct (Seedream 5.0 Pro). Inputs inline as base64.
            Pay-per-image.

Chosen per request by the ``provider`` param; see ``worker/flowstudio.py``.
"""
from __future__ import annotations

from .errors import BridgeEditError

__all__ = ["BridgeEditError"]
