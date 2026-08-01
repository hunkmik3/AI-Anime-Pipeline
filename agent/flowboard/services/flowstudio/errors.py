"""Terminal error for the Flow Studio image engines.

The three engines (`gemini_api`, `atrium_api`, `ark_api`) came over from the
manga_extract repo, where this type lived in `services/comic/bridge.py` — a
371-line Google-Flow browser-bridge module that none of these engines otherwise
touch (they are plain HTTP clients). Defining it here instead of importing it
keeps the studio free of the comic pipeline and of the bridge entirely.

The name is kept as ``BridgeEditError`` so the engine files stay byte-identical
to their source; only the import line changes. That matters while the two repos
still exist side by side — a diff should show the move, not a rewrite.
"""
from __future__ import annotations


class BridgeEditError(RuntimeError):
    """An image call could not produce a usable image, and retrying won't help.

    ``reason`` is a short machine-ish string (the upstream error, a safety block,
    a missing key); ``attempts`` is how many generate attempts were spent before
    giving up (0 for pre-flight failures). Raised — as opposed to a plain
    ``RuntimeError`` — to mean *stop*: the caller must not retry.
    """

    def __init__(self, reason: str, *, attempts: int) -> None:
        super().__init__(f"image generation failed after {attempts} attempt(s): {reason}")
        self.reason = reason
        self.attempts = attempts
