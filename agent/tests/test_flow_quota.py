"""The daily image cap, and what a comic run costs.

The cap existed as a number and never as a rule: `DAILY_QUOTA` appeared five
times in the whole repository and all five were inside the response body of the
usage meter. Running out showed "0 remaining" and the next generation went
through exactly as before.

So most of these are about the refusal actually happening, on BOTH entry points
— the panel workspace and the old studio each build their own request, and two
paths that disagreed about the ceiling would be worse than no ceiling.
"""
from __future__ import annotations

import pytest

from flowboard.db import get_session
from flowboard.db.models import Request
from flowboard.services import flow_quota as q
from flowboard.services import panel_service as ps
from flowboard.services import user_service


def _h(client, u, p="pw123456"):
    r = client.post("/api/account/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def panel(client):
    user_service.create_user("q_admin", "pw123456", role="admin")
    with get_session() as s:
        slate = ps.create_project(s, "Comics")
        comic = ps.create_series(s, slate.id, "COMIC")
        ch = ps.create_chapter(s, comic.id, "Ch 1")
        b = ps.create_batch(s, ch.id, "b")
        made = ps.import_panels(s, b.id, entries=[("P0.png", "raw-0")])
        pid = made[0].id
    return {"h": _h(client, "q_admin"), "id": pid}


def _spend(images: int, *, provider="atrium", status="done", panel_id=None):
    """Put `images` on today's tally the way a real run would."""
    with get_session() as s:
        s.add(
            Request(
                type="flow_gen_image",
                flow_panel_id=panel_id,
                status=status,
                params={"provider": provider, "variant_count": images},
                result=(
                    {"media_ids": [f"m{i}" for i in range(images)]}
                    if status == "done"
                    else {}
                ),
            )
        )
        s.commit()


def _gen(client, panel, n=1):
    return client.post(
        f"/api/flowstudio/panels/{panel['id']}/generate",
        json={"prompt": "x", "variant_count": n, "provider": "atrium"},
        headers=panel["h"],
    )


# ── the cap is one pool ─────────────────────────────────────────────────────


def test_every_atrium_model_shares_one_pool(client):
    """The ceiling belongs to the account, not the model name. Splitting it per
    model would either waste it or blow through it, depending which was busy."""
    _spend(3, provider="atrium")
    _spend(2, provider="atrium")
    with get_session() as s:
        assert q.used_today(s)[q.GEMINI] == 5


def test_seedream_does_not_touch_the_cap(client):
    """It is billed, not capped."""
    _spend(50, provider="avis")
    with get_session() as s:
        assert q.used_today(s)[q.GEMINI] == 0
        assert q.used_today(s)[q.SEEDREAM] == 50
        assert q.remaining(s) == q.DAILY_QUOTA


def test_the_default_ceiling_is_two_thousand(client):
    assert q.DAILY_QUOTA == 2000


# ── it actually refuses ─────────────────────────────────────────────────────


def test_generating_past_the_cap_is_refused(client, panel):
    """The whole point. This used to succeed."""
    _spend(q.DAILY_QUOTA, provider="atrium")
    r = _gen(client, panel)
    assert r.status_code == 429, r.text
    body = r.json()["detail"]
    assert body["used"] == q.DAILY_QUOTA
    assert body["quota"] == q.DAILY_QUOTA
    assert body["seconds_until_reset"] > 0


def test_a_batch_that_would_cross_the_line_is_refused_whole(client, panel):
    """Four variants with three left is not three images and a shortfall — the
    run either fits or it does not."""
    _spend(q.DAILY_QUOTA - 3, provider="atrium")
    assert _gen(client, panel, n=4).status_code == 429
    assert _gen(client, panel, n=3).status_code == 200


def test_refusing_leaves_no_request_behind(client, panel):
    """Checked before the row exists, so a refusal costs nothing and does not
    itself consume quota on the retry."""
    _spend(q.DAILY_QUOTA, provider="atrium")
    with get_session() as s:
        before = len(s.exec(__import__("sqlmodel").select(Request)).all())
    _gen(client, panel)
    with get_session() as s:
        after = len(s.exec(__import__("sqlmodel").select(Request)).all())
    assert after == before


def test_the_old_studio_path_obeys_the_same_cap(client, panel):
    """Two entry points that disagreed about the ceiling would be worse than no
    ceiling at all."""
    _spend(q.DAILY_QUOTA, provider="atrium")
    r = client.post(
        "/api/requests",
        json={"type": "flow_gen_image", "params": {"provider": "atrium", "variant_count": 1}},
        headers=panel["h"],
    )
    assert r.status_code == 429, r.text


def test_seedream_is_never_refused_by_the_cap(client, panel):
    _spend(q.DAILY_QUOTA, provider="atrium")
    r = client.post(
        f"/api/flowstudio/panels/{panel['id']}/generate",
        json={"prompt": "x", "variant_count": 1, "provider": "avis"},
        headers=panel["h"],
    )
    assert r.status_code == 200, r.text


# ── what counts ─────────────────────────────────────────────────────────────


def test_a_queued_run_reserves_its_share(client):
    """An in-flight request has no result yet. Counting only finished ones would
    let two people each pass the check on the last slot and both go through."""
    _spend(5, provider="atrium", status="queued")
    with get_session() as s:
        assert q.used_today(s)[q.GEMINI] == 5


def test_a_failed_run_costs_nothing(client):
    """The images were never made. Charging quota for an upstream error would
    punish the artist for it."""
    _spend(5, provider="atrium", status="error")
    with get_session() as s:
        assert q.used_today(s)[q.GEMINI] == 0


def test_a_finished_run_counts_what_it_produced_not_what_it_asked_for(client):
    with get_session() as s:
        s.add(
            Request(
                type="flow_gen_image", status="done",
                params={"provider": "atrium", "variant_count": 4},
                result={"media_ids": ["a", "b"]},   # only two came back
            )
        )
        s.commit()
    with get_session() as s:
        assert q.used_today(s)[q.GEMINI] == 2


# ── the tariff ──────────────────────────────────────────────────────────────


def test_seedream_is_priced_per_image_by_resolution(client):
    assert q.price_usd({"provider": "avis"}, 1) == q.SEEDREAM_USD_PER_IMAGE_1K
    assert q.price_usd({"provider": "avis", "resolution": "2K"}, 1) == q.SEEDREAM_USD_PER_IMAGE_2K
    assert q.price_usd({"provider": "avis"}, 4) == round(4 * q.SEEDREAM_USD_PER_IMAGE_1K, 6)


def test_a_4k_request_is_priced_as_2k(client):
    """The model clamps it, so charging a 4K rate would bill for something the
    studio did not receive."""
    assert q.price_usd({"provider": "avis", "resolution": "4K"}, 1) == q.SEEDREAM_USD_PER_IMAGE_2K


def test_an_atrium_image_costs_quota_not_money(client):
    assert q.price_usd({"provider": "atrium"}, 10) == 0.0


def test_the_retired_provider_counts_in_neither_bucket(client):
    """Dead history. Counting it as Atrium usage would be as wrong as counting
    it as Seedream spend."""
    _spend(9, provider="ark")
    with get_session() as s:
        assert q.used_today(s) == {q.GEMINI: 0, q.SEEDREAM: 0}


# ── it reaches the console ──────────────────────────────────────────────────


def test_spend_reaches_the_admin_rollup(client, panel):
    from flowboard.services import flow_stats as fs

    _spend(2, provider="avis", panel_id=panel["id"])
    rows = {r["name"]: r for r in fs.by_comic()}
    assert rows["COMIC"]["runs"] == 2
    assert rows["COMIC"]["spent_usd"] == round(2 * q.SEEDREAM_USD_PER_IMAGE_1K, 4)


def test_the_console_reports_the_cap_alongside_the_spend(client, panel):
    _spend(7, provider="atrium")
    got = client.get("/api/admin/stats/comics/quota", headers=panel["h"]).json()
    assert got["quota"] == 2000
    assert got["used"] == 7
    assert got["remaining"] == 1993
