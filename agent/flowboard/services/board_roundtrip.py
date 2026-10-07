"""Keep stored JSON number types across semantically unchanged browser saves."""
from __future__ import annotations

from typing import Any


def preserve_numeric_representation(incoming: Any, stored: Any) -> Any:
    """Preserve equal int/float values without suppressing actual board edits.

    JavaScript serializes whole-valued floats such as 20.0 as 20. Keeping the
    stored representation avoids invalidating JSON-based production hashes on
    autosave. Compare exactly, and exclude bool even though it subclasses int.
    Incoming keys, list order, deletions, and all changed values still win.
    """
    if isinstance(incoming, dict) and isinstance(stored, dict):
        return {
            key: preserve_numeric_representation(value, stored.get(key))
            for key, value in incoming.items()
        }
    if isinstance(incoming, list) and isinstance(stored, list):
        return [
            preserve_numeric_representation(value, stored[index])
            if index < len(stored) else value
            for index, value in enumerate(incoming)
        ]
    if type(incoming) in (int, float) and type(stored) in (int, float) and incoming == stored:
        return stored
    return incoming
