"""Fill a DEMO database with plausible production activity.

The tracker, the budget panels and the review queue all read from recorded
history, so on a fresh install they are honestly empty — which makes it hard to
tell whether they *work*. This writes a season's worth of believable activity so
those screens can be judged on how they behave with real numbers in them.

Everything it writes is tagged (see ``TAG``) and ``--undo`` removes exactly that,
so a demo can be reset without touching anything a human entered.

    # look first — prints what it would do, writes nothing
    python scripts/seed_demo_activity.py --dry-run

    python scripts/seed_demo_activity.py
    python scripts/seed_demo_activity.py --undo

SAFETY: refuses to run unless the database name contains "demo" or "test", and
refuses outright on the production database name. Seeding real studio data with
invented submissions would corrupt the record the app exists to keep.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlmodel import select  # noqa: E402

from flowboard.db import get_session  # noqa: E402
from flowboard.db.models import (  # noqa: E402
    DownloadEvent,
    Node,
    Project,
    Request,
    Scene,
    Series,
    Shot,
    Submission,
    UsageRecord,
    User,
)

#: Stamped into every row this script creates, so --undo can find them again.
TAG = "seeded_demo_activity"

#: Deterministic, so a re-run tells the same story instead of a different one.
SEED = 20260729

# Costs in the range the studio actually sees per clip (Avis / Seedance).
COST_PER_CLIP = (0.32, 0.48)

_BLOCKED_DB = "flowboard_server"


def _guard(url: str, force: bool) -> None:
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if name == _BLOCKED_DB:
        sys.exit(
            f"refusing to seed {name!r} — that is the production database.\n"
            "Point FLOWBOARD_DATABASE_URL at the demo database first."
        )
    if not force and not any(k in name.lower() for k in ("demo", "test")):
        sys.exit(
            f"refusing to seed {name!r}: the name doesn't look like a demo/test "
            "database. Re-run with --force if you are certain."
        )
    print(f"database: {name}")


# ── undo ────────────────────────────────────────────────────────────────────


def undo() -> None:
    with get_session() as s:
        subs = list(s.exec(select(Submission)).all())
        gone_subs = 0
        scene_ids = set()
        for row in subs:
            if row.note and TAG in row.note:
                scene_ids.add(row.scene_id)
                s.delete(row)
                gone_subs += 1

        reqs = list(s.exec(select(Request)).all())
        gone_reqs = 0
        node_ids = set()
        seeded_req_ids = set()
        for r in reqs:
            if (r.params or {}).get("_tag") == TAG:
                if r.node_id:
                    node_ids.add(r.node_id)
                if r.id is not None:
                    seeded_req_ids.add(r.id)
                gone_reqs += 1

        # Usage rows first: they reference the requests, and leaving them behind
        # would keep the money on the admin Cost screens after a reset.
        gone_usage = 0
        for u in s.exec(select(UsageRecord)).all():
            if u.request_id in seeded_req_ids:
                s.delete(u)
                gone_usage += 1
        for r in reqs:
            if (r.params or {}).get("_tag") == TAG:
                s.delete(r)
        s.commit()

        gone_dl = 0
        for d in s.exec(select(DownloadEvent)).all():
            if d.node_id in node_ids:
                s.delete(d)
                gone_dl += 1

        gone_nodes = 0
        for nid in node_ids:
            n = s.get(Node, nid)
            if n is not None and (n.data or {}).get("_tag") == TAG:
                s.delete(n)
                gone_nodes += 1

        # Return the episodes we touched to an untouched state.
        for sid in scene_ids:
            sc = s.get(Scene, sid)
            if sc is not None:
                sc.deliverable_status = "draft"
                s.add(sc)
        s.commit()

    print(
        f"removed {gone_subs} submission(s), {gone_reqs} request(s), "
        f"{gone_usage} usage row(s), {gone_dl} download(s), {gone_nodes} node(s); "
        f"{len(scene_ids)} episode(s) reset to draft"
    )
    print("NOTE: assignees and budgets are left in place — they are ordinary")
    print("      settings a human would also make, not invented history.")


# ── seed ────────────────────────────────────────────────────────────────────


def seed(dry_run: bool) -> None:
    rnd = random.Random(SEED)
    now = datetime.now(timezone.utc)

    with get_session() as s:
        projects = list(s.exec(select(Project)).all())
        if not projects:
            sys.exit("no projects in this database — nothing to seed")

        artists = [
            u
            for u in s.exec(select(User)).all()
            if u.role != "admin" and getattr(u, "status", "active") == "active"
        ]
        if len(artists) < 3:
            sys.exit("need at least 3 non-admin users to spread work across")
        artists = artists[:6]
        reviewer = next((u for u in s.exec(select(User)).all() if u.role == "admin"), None)
        if reviewer is None:
            sys.exit("need an admin account to act as the reviewer")

        print(f"artists : {', '.join(a.username for a in artists)}")
        print(f"reviewer: {reviewer.username}")

        made = {"assigned": 0, "shots": 0, "nodes": 0, "gens": 0,
                "downloads": 0, "subs": 0, "usd": 0.0}

        for project in projects:
            all_series = list(
                s.exec(select(Series).where(Series.project_id == project.id)).all()
            )
            # A project-wide ceiling, generous enough that the series ones bind first.
            _set_budget(project, "settings", 4000.0)
            if not dry_run:
                s.add(project)

            for series in all_series:
                _set_budget(series, "production", 1200.0)
                # A producer so the approver chain has its first link.
                if series.producer_user_id is None:
                    series.producer_user_id = reviewer.id
                if not dry_run:
                    s.add(series)

                episodes = sorted(
                    s.exec(select(Scene).where(Scene.series_id == series.id)).all(),
                    key=lambda e: (e.order_index, e.name),
                )
                # Only the first stretch of each series gets history: a real studio
                # is part-way through, and a 100%-complete board would hide exactly
                # the states (in review, sent back, unstaffed) worth looking at.
                worked = episodes[: max(6, len(episodes) // 3)]

                for i, ep in enumerate(worked):
                    artist = artists[i % len(artists)]
                    if ep.assignee_user_id is None:
                        ep.assignee_user_id = artist.id
                        made["assigned"] += 1
                    else:
                        artist = s.get(User, ep.assignee_user_id) or artist
                    _set_budget(ep, "production", 90.0)

                    # ── generation history: shots → nodes → requests with cost
                    shots = list(s.exec(select(Shot).where(Shot.scene_id == ep.id)).all())
                    if not shots and not dry_run:
                        for k in range(rnd.randint(4, 8)):
                            sh = Shot(
                                scene_id=ep.id,
                                order_index=k,
                                code=f"SQ{k + 1:02d}",
                            )
                            s.add(sh)
                            s.flush()
                            shots.append(sh)
                            made["shots"] += 1

                    for sh in shots:
                        # A sequence holds a few shot slots (nodes), and an artist
                        # regenerates a slot until they like it. Modelling it that
                        # way matters: the waste figure is computed per node as
                        # "last take kept, earlier takes wasted", so one take per
                        # node would report 0% waste — reading as "nothing wasted"
                        # when it really means "we have no takes to compare".
                        for slot in range(rnd.randint(1, 3)):
                            takes = rnd.choices([1, 2, 3, 4], weights=[45, 30, 17, 8])[0]
                            if dry_run:
                                made["gens"] += takes
                                made["usd"] += takes * sum(COST_PER_CLIP) / 2
                                continue

                            node = Node(
                                shot_id=sh.id,
                                short_id=f"d{rnd.randrange(16**6):06x}",
                                type="video",
                                x=float(slot * 260),
                                y=0.0,
                                status="done",
                                data={
                                    "_tag": TAG,
                                    "prompt": f"{ep.code or ep.name} {sh.code or 'SQ'} slot {slot + 1}",
                                },
                            )
                            s.add(node)
                            s.flush()
                            made["nodes"] += 1

                            for take in range(takes):
                                when = now - timedelta(
                                    days=rnd.randint(3, 40), hours=rnd.randint(0, 23)
                                )
                                cost = round(rnd.uniform(*COST_PER_CLIP), 4)
                                made["gens"] += 1
                                made["usd"] += cost
                                media_id = f"{rnd.randrange(16**24):024x}"
                                req = Request(
                                    node_id=node.id,
                                    type="gen_video",
                                    params={"_tag": TAG, "duration_seconds": 15},
                                    status="done",
                                    result={"cost_usd": cost, "media_id": media_id},
                                    created_at=when,
                                    finished_at=when + timedelta(minutes=rnd.randint(2, 9)),
                                )
                                s.add(req)
                                s.flush()
                                # Two readers, two sources: the budget gate and the
                                # tracker total the cost off Request.result, while
                                # the admin Cost/Spend screens sum settled
                                # UsageRecord rows. Writing only one left the
                                # tracker showing $207 and Cost breakdown $0.
                                s.add(
                                    UsageRecord(
                                        user_id=artist.id,
                                        request_id=req.id,
                                        kind="video",
                                        model="seedance-2-0",
                                        estimated_usd=cost,
                                        actual_usd=cost,
                                        status="settled",
                                        created_at=when,
                                        settled_at=when + timedelta(minutes=rnd.randint(2, 9)),
                                    )
                                )
                                # The download button is the only "this one was
                                # actually used" signal the app gets, and it lands
                                # on the take the artist settled on. Not every slot
                                # gets one — people forget — which is itself a real
                                # condition the waste figure has to cope with.
                                if take == takes - 1 and rnd.random() < 0.7:
                                    s.add(
                                        DownloadEvent(
                                            user_id=artist.id,
                                            media_id=media_id,
                                            node_id=node.id,
                                            created_at=when + timedelta(hours=1),
                                        )
                                    )
                                    made["downloads"] += 1

                    # Leave episodes that already have a delivery record alone.
                    # Writing v1/v2 on top of existing rows collided with their
                    # version numbers and produced several submissions "awaiting
                    # review" for one episode — a state the real flow can't reach,
                    # since only the newest attempt is ever open.
                    existing = s.exec(
                        select(Submission).where(Submission.scene_id == ep.id)
                    ).first()
                    if existing is not None:
                        continue

                    # ── delivery history: a believable mix of outcomes
                    outcome = _outcome(i, rnd)
                    if not dry_run:
                        made["subs"] += _write_delivery(
                            s, ep, artist, reviewer, outcome, now, rnd
                        )
                    else:
                        made["subs"] += 2 if outcome == "resubmitted" else 1
                    if not dry_run:
                        s.add(ep)

            if not dry_run:
                s.commit()

    verb = "would create" if dry_run else "created"
    print(
        f"\n{verb}: {made['assigned']} assignment(s), {made['shots']} sequence(s), "
        f"{made['gens']} generation(s) across {made['nodes']} shot slot(s) "
        f"costing ${made['usd']:.2f}, {made['downloads']} download(s), "
        f"{made['subs']} submission(s)"
    )
    if dry_run:
        print("(dry run — nothing was written)")
    else:
        print("re-run with --undo to remove the invented history")


def _outcome(i: int, rnd: random.Random) -> str:
    """Spread outcomes so every state on the board is represented.

    A board where everything is approved shows none of the states a producer
    actually needs to see, so the mix is deliberate rather than random: roughly
    half land first time, a quarter come back once, and the rest sit unfinished.
    """
    return ["approved", "resubmitted", "in_review", "approved", "sent_back", "draft"][i % 6]


def _write_delivery(s, ep, artist, reviewer, outcome, now, rnd) -> int:
    """Write the Submission rows for one episode's outcome. Returns how many."""
    drive = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view"
    base = now - timedelta(days=rnd.randint(5, 30))
    n = 0

    def submission(version, status, submitted_at, reviewed_at=None, review_note=None):
        nonlocal n
        n += 1
        s.add(
            Submission(
                scene_id=ep.id,
                version=version,
                drive_url=drive,
                drive_file_id="1AbCdEfGhIjKlMnOpQrStUv",
                # The tag lives in the note so --undo can find it again.
                note=f"cut v{version} — {TAG}",
                submitted_by=artist.id,
                submitted_at=submitted_at,
                status=status,
                approver_user_id=reviewer.id,
                reviewed_by=reviewer.id if reviewed_at else None,
                reviewed_at=reviewed_at,
                review_note=review_note,
            )
        )

    if outcome == "draft":
        ep.deliverable_status = "draft"
        return 0

    if outcome == "in_review":
        submission(1, "submitted", base)
        ep.deliverable_status = "submitted"
        return n

    if outcome == "sent_back":
        submission(
            1, "rejected", base, base + timedelta(days=1),
            "Lip sync drifts from 00:08. Please regenerate that sequence.",
        )
        ep.deliverable_status = "draft"  # a rejection returns it to the assignee
        return n

    if outcome == "resubmitted":
        submission(
            1, "rejected", base, base + timedelta(days=1),
            "Colour grade doesn’t match the series look — too warm.",
        )
        submission(
            2, "approved", base + timedelta(days=3),
            base + timedelta(days=4), "Matches now, good.",
        )
        ep.deliverable_status = "approved"
        return n

    # approved first time
    submission(1, "approved", base, base + timedelta(days=1), "Clean, no notes.")
    ep.deliverable_status = "approved"
    return n


def _set_budget(obj, attr: str, amount: float) -> None:
    """Write a ceiling. Assigns rather than setdefault: a leftover value from an
    earlier run would otherwise survive and leave the demo showing spend above its
    own ceiling — a state the real gate makes unreachable, so showing it would
    misrepresent how the app behaves."""
    bag = dict(getattr(obj, attr) or {})
    bag["credit_budget_usd"] = round(amount, 2)
    setattr(obj, attr, bag)


def rebudget() -> None:
    """Set every ceiling from what was actually spent under it.

    Seeded history is written straight to the tables, bypassing the gate that
    normally makes overspend impossible. Left alone, the demo shows $199 spent
    against a $100 ceiling — an impossible reading that teaches the wrong thing
    about the app. This derives each ceiling from its own measured spend instead,
    and leaves one series deliberately tight so the "running out" state is
    visible too.
    """
    from flowboard.services import scope_budget

    with get_session() as s:
        for project in s.exec(select(Project)).all():
            spent = scope_budget.spend_usd(s, "project", project.id)["spent_usd"]
            _set_budget(project, "settings", max(50.0, spent * 4))
            s.add(project)

            all_series = list(
                s.exec(select(Series).where(Series.project_id == project.id)).all()
            )
            for idx, series in enumerate(all_series):
                sp = scope_budget.spend_usd(s, "series", series.id)["spent_usd"]
                # One series near its ceiling, so the warning state is on screen.
                factor = 1.15 if idx == 0 else 3.0
                _set_budget(series, "production", max(20.0, sp * factor))
                s.add(series)

                for ep in s.exec(select(Scene).where(Scene.series_id == series.id)).all():
                    esp = scope_budget.spend_usd(s, "scene", ep.id)["spent_usd"]
                    if esp <= 0:
                        continue
                    _set_budget(ep, "production", max(10.0, esp * 2.5))
                    s.add(ep)
        s.commit()

        print("ceilings set from measured spend:")
        for project in s.exec(select(Project)).all():
            b = scope_budget.summary(s, "project", project.id)
            print(f"  {project.name}: ${b['spent_usd']:.2f} of ${b['effective_usd']:.0f}"
                  f"  ({b['used_pct']}%)")
            for series in s.exec(
                select(Series).where(Series.project_id == project.id)
            ).all():
                sb = scope_budget.summary(s, "series", series.id)
                print(f"    {series.code or series.name}: ${sb['spent_usd']:.2f} of "
                      f"${sb['effective_usd']:.0f}  ({sb['used_pct']}%)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--undo", action="store_true", help="remove what this script created")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument(
        "--rebudget",
        action="store_true",
        help="only reset ceilings from measured spend (no new history)",
    )
    ap.add_argument("--force", action="store_true", help="allow a non-demo database name")
    args = ap.parse_args()

    url = os.getenv("FLOWBOARD_DATABASE_URL", "")
    if not url:
        sys.exit("set FLOWBOARD_DATABASE_URL first")
    _guard(url, args.force)

    if args.undo:
        undo()
    elif args.rebudget:
        rebudget()
    else:
        seed(args.dry_run)
        if not args.dry_run:
            print()
            rebudget()


if __name__ == "__main__":
    main()
