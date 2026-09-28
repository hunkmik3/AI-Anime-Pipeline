"""Read source-agent JSON with one narrowly bounded delimiter correction.

This is syntax recovery only. It never supplies a missing value, field, closing
delimiter, or evidence ID; callers must still apply the entire source contract.
"""

from __future__ import annotations

import hashlib
import json
import re


def _single_closer(text: str) -> tuple[str, dict] | None:
    """Substitute exactly one wrongly typed closer, without adding/removing bytes."""
    stack: list[str] = []
    quoted = escaped = False
    mismatch: tuple[int, str, str] | None = None
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack:
                return None
            expected = {"[": "]", "{": "}"}[stack.pop()]
            if char != expected:
                if mismatch is not None:
                    return None
                mismatch = (index, char, expected)
    if stack or quoted or mismatch is None:
        return None
    index, old, new = mismatch
    return text[:index] + new + text[index + 1:], {
        "policy": "single_mismatched_closer_v1", "offset": index,
        "from": old, "to": new,
    }


def parse_object(text: str) -> tuple[dict, dict | None]:
    """Return an object and optional auditable, one-character syntax repair.

    Valid fenced/prefixed objects retain the existing reader's compatibility.
    Repair requires the *entire candidate* to parse, including its closing root;
    a truncated or otherwise malformed response is never completed heuristically.
    """
    raw = text
    offset = len(text) - len(text.lstrip())
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", candidate, re.DOTALL)
    if fence:
        inner = fence.group(1)
        offset += fence.start(1) + len(inner) - len(inner.lstrip())
        candidate = inner.strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as initial_error:
        start = candidate.find("{")
        if start < 0:
            raise ValueError("source agent did not return a JSON object") from initial_error
        candidate = candidate[start:]
        offset += start
        try:
            parsed, _ = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError:
            correction = _single_closer(candidate)
            if correction is None:
                raise initial_error
            repaired, details = correction
            # Strictly parse the whole corrected value, not a partial prefix.
            parsed = json.loads(repaired)
            if not isinstance(parsed, dict):
                raise ValueError("source agent did not return a JSON object")
            details["offset"] += offset
            corrected_raw = raw[:details["offset"]] + details["to"] + raw[details["offset"] + 1:]
            details.update(
                raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                repaired_sha256=hashlib.sha256(corrected_raw.encode()).hexdigest(),
            )
            return parsed, details
    if not isinstance(parsed, dict):
        raise ValueError("source agent did not return a JSON object")
    return parsed, None
