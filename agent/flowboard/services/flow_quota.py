"""The daily image cap and the Seedream tariff — one definition, three readers.

These numbers lived inside `routes/flowstudio.py`, which is the endpoint that
*displays* them. Nothing else could see them, so the cap was a figure on a meter
and never a rule: `DAILY_QUOTA` appeared five times in the whole repository and
all five were in that one response body. Running out showed "0 remaining" and
the next generation went through exactly as before.

Three things read this now, and they must agree or the meter is lying:

  * the usage meter, which shows what is left;
  * both generation paths, which refuse when it is gone;
  * the admin cost rollup, which prices what was spent.

**Two engines, one cap.** Every Atrium model — all the Nano Banana variants —
counts against a single pool, because the upstream limit is on the account and
not on the model name. Seedream is not capped; it is billed, at a fixed
per-image tariff.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from flowboard.db.models import Request

#: Images per day across ALL Atrium/banana models together. Not per model: the
#: ceiling belongs to the account, so splitting it per model would either
#: under-use it or blow through it, depending on which model was busy.
DAILY_QUOTA = int(os.getenv("FLOWBOARD_DAILY_QUOTA", "2000"))

#: Seedream is priced, not capped. Fixed tariff — this is what the studio is
#: charged per image, not a figure the gateway reports back.
SEEDREAM_USD_PER_IMAGE_1K = float(os.getenv("FLOWBOARD_SEEDREAM_USD_PER_IMAGE_1K", "0.059125"))
SEEDREAM_USD_PER_IMAGE_2K = float(os.getenv("FLOWBOARD_SEEDREAM_USD_PER_IMAGE_2K", "0.11825"))

GEMINI = "gemini"
SEEDREAM = "seedream"


class QuotaExceeded(Exception):
    """Today's Atrium images are gone. Carries what the caller needs to say so."""

    def __init__(self, used: int, quota: int, seconds_until_reset: int) -> None:
        super().__init__(
            f"today's image quota is used up ({used}/{quota}) — "
            f"it resets in {seconds_until_reset // 3600}h "
            f"{(seconds_until_reset % 3600) // 60}m"
        )
        self.used = used
        self.quota = quota
        self.seconds_until_reset = seconds_until_reset


# ── classifying a run ───────────────────────────────────────────────────────


def engine_of(params: object) -> Optional[str]:
    """Which pool a run belongs to.

    ``avis`` → seedream. The decommissioned direct-BytePlus ``ark`` provider →
    None, meaning excluded from every bucket rather than relabelled: those rows
    are dead history, and counting them as Atlas usage would be as wrong as
    counting them as Seedream spend.
    """
    p = ""
    if isinstance(params, dict):
        p = str(params.get("provider") or "").lower()
    if p == "avis":
        return SEEDREAM
    if p == "ark":
        return None
    return GEMINI


def images_in(result: object) -> int:
    if not isinstance(result, dict):
        return 0
    mids = result.get("media_ids")
    if not isinstance(mids, list):
        return 0
    return sum(1 for m in mids if isinstance(m, str) and m)


def resolution_of(params: object) -> str:
    """"2K" when the request asked for 2K or 4K (which this model clamps to 2K)."""
    if isinstance(params, dict):
        r = str(params.get("resolution") or params.get("size") or "").upper()
        if "2K" in r or "4K" in r:
            return "2K"
    return "1K"


def wanted_images(params: object) -> int:
    """How many images a run is ASKING for.

    An in-flight request has no result yet, and counting only finished ones
    would let two people each pass the check on the last slot and both go
    through. A queued request reserves its share the moment it exists.
    """
    if isinstance(params, dict):
        try:
            return max(1, int(params.get("variant_count") or 1))
        except (TypeError, ValueError):
            return 1
    return 1


def price_usd(params: object, images: int) -> float:
    """What ``images`` at this request's settings cost. Seedream only — an
    Atrium image costs nothing and spends quota instead."""
    if engine_of(params) != SEEDREAM or images <= 0:
        return 0.0
    per = (
        SEEDREAM_USD_PER_IMAGE_2K
        if resolution_of(params) == "2K"
        else SEEDREAM_USD_PER_IMAGE_1K
    )
    return round(images * per, 6)


# ── the day ─────────────────────────────────────────────────────────────────


def _day_bounds() -> tuple[datetime, datetime]:
    """Local midnight to the next one. Local, not UTC: "today" is the studio's
    working day, and a cap that rolled over mid-afternoon would be a surprise
    every single day."""
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def used_today(session: Session) -> dict[str, int]:
    """Images spent since local midnight, per pool.

    A finished run counts what it produced; anything still queued or running
    counts what it asked for. Failed runs count nothing — the images were never
    made, and charging quota for an upstream error would punish the artist for
    it.
    """
    start, _ = _day_bounds()
    rows = session.exec(
        select(Request).where(Request.type == "flow_gen_image")
    ).all()
    out = {GEMINI: 0, SEEDREAM: 0}
    for r in rows:
        created = _aware(r.created_at)
        if created is None or created < start:
            continue
        eng = engine_of(r.params)
        if eng is None or r.status == "error":
            continue
        out[eng] += images_in(r.result) if r.status == "done" else wanted_images(r.params)
    return out


def seconds_until_reset() -> int:
    now = datetime.now().astimezone()
    _, end = _day_bounds()
    return max(0, int((end - now).total_seconds()))


def remaining(session: Session) -> int:
    return max(0, DAILY_QUOTA - used_today(session)[GEMINI])


def check(session: Session, params: object) -> None:
    """Refuse a run that would take the pool past its cap.

    Checked BEFORE the request row exists, so a refusal costs nothing and leaves
    nothing behind. Seedream passes straight through — it is billed, not capped,
    and the thing that stops runaway spend there is the budget, not this.
    """
    if engine_of(params) != GEMINI:
        return
    used = used_today(session)[GEMINI]
    if used + wanted_images(params) > DAILY_QUOTA:
        raise QuotaExceeded(used, DAILY_QUOTA, seconds_until_reset())
