"""USD pricing config for video providers.

The Dreamina contract (docs/dreamina_api_contract.md §5) reports billing
in raw tokens. The USD-per-1M-tokens rate is "TBD" until the user looks
it up on the BytePlus console, so we keep the rate in a separate config
file:

    ~/.flowboard/pricing.json

```json
{
  "video": {
    "seedance-1-5-pro": {"usd_per_million_tokens": 0.0},
    "seedance-2-0":     {"usd_per_million_tokens": 0.0},
    "flow-default":     {"usd_per_clip": 0.0}
  }
}
```

When ``usd_per_million_tokens`` (or ``usd_per_clip``) is 0 / missing, the
provider reports ``cost_usd = 0.0`` and the UI shows a "Pricing not
configured" badge instead of a misleading $0.00.

Why a separate file from ``secrets.json``:

- Pricing is non-secret config; secrets need ``chmod 600`` and don't.
- Pricing changes more often (per-model rates evolve) than API keys do.
- Mixing them means rotating an API key requires touching a pricing
  block too. Single-responsibility.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_DEFAULT_PATH = Path.home() / ".flowboard" / "pricing.json"


def _path() -> Path:
    override = os.environ.get("FLOWBOARD_PRICING_PATH")
    return Path(override) if override else _DEFAULT_PATH


def _read() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("pricing: file unreadable, treating as empty (%s)", exc)
        return {}


def get_video_rate(model_id: str) -> dict:
    """Return the pricing block for ``model_id``. Empty dict when unset.

    The shape depends on the model — Dreamina uses ``usd_per_million_tokens``,
    Flow (clip-based) would use ``usd_per_clip``. The provider knows
    which keys to read; the caller just passes the model id.
    """
    doc = _read()
    block = (doc.get("video") or {}).get(model_id)
    return block if isinstance(block, dict) else {}


def compute_cost_usd(model_id: str, *, tokens: Optional[int] = None) -> float:
    """Best-effort USD computation.

    Returns 0.0 when the rate isn't configured for this model. The UI
    treats 0.0 + nonzero ``cost_tokens`` as "pricing not configured" and
    prompts the user to set the rate in pricing.json.
    """
    rate_cfg = get_video_rate(model_id)
    if tokens is not None and tokens > 0:
        per_million = rate_cfg.get("usd_per_million_tokens")
        if isinstance(per_million, (int, float)) and per_million > 0:
            return float(tokens) / 1_000_000.0 * float(per_million)
    per_clip = rate_cfg.get("usd_per_clip")
    if isinstance(per_clip, (int, float)) and per_clip > 0:
        return float(per_clip)
    return 0.0
