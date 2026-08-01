"""Snapshot the running app's real DOM into ONE self-contained HTML file.

The earlier export hand-wrote each screen's markup from the components, which drifts
the moment a component changes — and the whole point of a design export is that it
matches. This drives a headless Chrome over the DevTools Protocol instead: it signs
in, visits each route, waits for the data to land, and takes the DOM the browser
actually built. So it is the app, not a reconstruction of it.

Output is a single file with every screen inlined as a section, a switcher across the
top, and the stylesheet embedded — no server, no build step, nothing to serve.

    python scripts/snapshot_design.py                 # → ../design-export/app.html
    python scripts/snapshot_design.py --user pm_test   # snapshot as someone else

Needs the app running (frontend + backend) and Google Chrome installed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "design-export" / "app.html"
CSS = ROOT / "frontend" / "src" / "styles.css"
FAVICON = ROOT / "frontend" / "public" / "favicon.png"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

#: Route → (label, what to wait for). The selector matters: without it the snapshot
#: catches the loading state, which is the one view nobody needs to design.
#: The object pages need real ids, so they are filled in at run time from the
#: database rather than hardcoded — a stale id snapshots an error page.
SCREENS: list[tuple[str, str, str]] = [
    ("/projects", "Projects", ".project-card, .page-empty"),
    ("/projects/{project}", "Project home", ".series-block, .page-empty"),
    ("/projects/{project}/series/{series}", "Series", ".ep__facts, .ep__err"),
    ("/projects/{project}/episodes/{episode}", "Episode", ".ep__facts, .ep__err"),
    ("/work", "Work", ".inbox, .inbox__empty"),
    ("/review", "Review", ".inbox, .inbox__empty"),
    ("/admin?tab=members", "Admin · Members", ".dash__main table, .dash__main .admin2__card"),
    ("/admin?tab=approvals", "Admin · Approvals", ".dash__main"),
    ("/admin?tab=spend&view=summary", "Admin · Spend summary", ".stats, .pool"),
    ("/admin?tab=spend&view=delivery", "Admin · Delivery", ".rtable, .rfoot"),
    ("/admin?tab=spend&view=projects", "Admin · Cost tree", ".ctree, .admin2__empty"),
    ("/admin?tab=spend&view=ledger", "Admin · Spend ledger", ".rtable, .rfoot"),
    ("/admin?tab=production", "Admin · Series & episodes", ".crm-table, .admin2__card"),
    ("/admin?tab=audit", "Admin · Audit log", ".dash__main"),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def login(base: str, username: str, password: str) -> str:
    req = urllib.request.Request(
        f"{base}/api/account/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["token"]


async def snapshot(front: str, token: str, extra: list[tuple[str, str, str]]) -> list[dict]:
    import websockets

    port = free_port()
    profile = tempfile.mkdtemp(prefix="design-snap-")
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--window-size=1600,1100",
            "--hide-scrollbars",
            "--no-first-run",
            "--disable-gpu",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        ws_url = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                    ws_url = json.loads(r.read())["webSocketDebuggerUrl"]
                break
            except Exception:
                time.sleep(0.5)
        if not ws_url:
            sys.exit("Chrome didn't expose a debugging endpoint")

        async with websockets.connect(ws_url, max_size=80 * 1024 * 1024) as ws:
            counter = {"n": 0}

            async def cmd(method: str, params: dict | None = None, session: str | None = None):
                counter["n"] += 1
                msg = {"id": counter["n"], "method": method, "params": params or {}}
                if session:
                    msg["sessionId"] = session
                await ws.send(json.dumps(msg))
                while True:
                    data = json.loads(await ws.recv())
                    if data.get("id") == counter["n"]:
                        if "error" in data:
                            raise RuntimeError(f"{method}: {data['error']}")
                        return data.get("result", {})

            targets = await cmd("Target.getTargets")
            page_id = next(
                t["targetId"] for t in targets["targetInfos"] if t["type"] == "page"
            )
            session = (
                await cmd("Target.attachToTarget", {"targetId": page_id, "flatten": True})
            )["sessionId"]
            await cmd("Page.enable", session=session)
            await cmd("Runtime.enable", session=session)

            async def goto(url: str) -> None:
                await cmd("Page.navigate", {"url": url}, session)
                await asyncio.sleep(0.4)

            async def evaluate(expr: str):
                r = await cmd(
                    "Runtime.evaluate",
                    {"expression": expr, "awaitPromise": True, "returnByValue": True},
                    session,
                )
                return r.get("result", {}).get("value")

            # Seed the session token on the app's own origin, then reload so the SPA
            # boots already signed in — otherwise every route renders the login page.
            await goto(front)
            await evaluate(f"localStorage.setItem('flowboard_token', {json.dumps(token)})")

            out: list[dict] = []
            for path, label, wait_for in extra:
                await goto(front + path)
                # Wait for the screen's own content, not just load: these pages fetch
                # in an effect, so "loaded" still means empty.
                ok = await evaluate(
                    """(async () => {
                      const sel = %s;
                      for (let i = 0; i < 60; i++) {
                        if (document.querySelector(sel)) return true;
                        await new Promise(r => setTimeout(r, 250));
                      }
                      return false;
                    })()"""
                    % json.dumps(wait_for)
                )
                # Let late-arriving panels (budgets, per-row fetches) settle.
                await asyncio.sleep(1.2)
                body = await evaluate(
                    """(() => {
                      const root = document.getElementById('root');
                      if (!root) return '';
                      // Drop the toast host and any open portal: they are transient
                      // state, not part of the screen being designed.
                      const clone = root.cloneNode(true);
                      clone.querySelectorAll('[data-sonner-toaster],.toaster,.Toaster')
                        .forEach(n => n.remove());
                      return clone.innerHTML;
                    })()"""
                )
                print(f"  {'✓' if ok else '~'} {label:28} {len(body or ''):>7} bytes")
                out.append({"path": path, "label": label, "html": body or "", "ready": bool(ok)})
            return out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def build(screens: list[dict], css: str, favicon_b64: str) -> str:
    # Rewrite the favicon reference to the inlined copy so the file stands alone.
    fav = f"data:image/png;base64,{favicon_b64}" if favicon_b64 else ""
    sections = ""
    buttons = ""
    for i, s in enumerate(screens):
        body = s["html"]
        if fav:
            body = body.replace('src="/favicon.png"', f'src="{fav}"')
        # Neutralise navigation: this is one file, so links must not try to route.
        body = re.sub(r'href="/[^"]*"', 'href="javascript:void 0"', body)
        sections += (
            f'<section class="xs" id="s{i}"{"" if i == 0 else " hidden"}'
            f' data-label="{s["label"]}">{body}</section>'
        )
        buttons += (
            f'<button class="xb{" is-on" if i == 0 else ""}" data-i="{i}">'
            f'{s["label"]}</button>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Giant Studio — every screen</title>
{f'<link rel="icon" href="{fav}">' if fav else ''}
<style>
{css}
</style>
<style>
  /* Export-only chrome. Prefixed .x so it can't collide with the app's classes,
     and fixed at the top so switching screens never scrolls out of reach. */
  .xbar {{
    position: sticky; top: 0; z-index: 99999;
    display: flex; gap: 4px; flex-wrap: wrap; align-items: center;
    padding: 8px 12px;
    background: #0b0f14; border-bottom: 1px solid #2b3640;
    font: 12px/1.4 ui-sans-serif, system-ui, sans-serif;
  }}
  .xbar strong {{ color: #e7ecf0; font-size: 12px; margin-right: 8px; letter-spacing: -0.01em; }}
  .xb {{
    background: #161d25; color: #9aa7b4; border: 1px solid #2b3640;
    border-radius: 7px; padding: 5px 10px; font: inherit; cursor: pointer;
    white-space: nowrap;
  }}
  .xb:hover {{ color: #e7ecf0; border-color: #3d4a57; }}
  .xb.is-on {{ background: rgba(0,167,111,.16); border-color: rgba(0,167,111,.5); color: #5be49b; font-weight: 600; }}
  .xhint {{ margin-left: auto; color: #56626f; font-size: 11px; }}
  /* Each screen is a full app shell, so give it its own viewport-sized box rather
     than letting them all fight over 100vh. */
  .xs {{ position: relative; min-height: calc(100vh - 40px); }}
  .xs[hidden] {{ display: none; }}
  .xs .app, .xs .dash {{ min-height: calc(100vh - 40px); height: auto; width: auto; }}
  .xs .topbar, .xs .account-menu {{ position: static; }}
</style>
</head>
<body>
<div class="xbar">
  <strong>Giant Studio — screens</strong>
  {buttons}
  <span class="xhint">real DOM, captured from the running app</span>
</div>
{sections}
<script>
  const btns = [...document.querySelectorAll('.xb')];
  const secs = [...document.querySelectorAll('.xs')];
  function show(i) {{
    secs.forEach((s, n) => s.hidden = n !== i);
    btns.forEach((b, n) => b.classList.toggle('is-on', n === i));
    location.hash = '#' + i;
    window.scrollTo(0, 0);
  }}
  btns.forEach(b => b.addEventListener('click', () => show(+b.dataset.i)));
  // Keep the chosen screen across a reload, and allow ←/→ to step through them.
  const start = parseInt(location.hash.slice(1), 10);
  if (Number.isInteger(start) && secs[start]) show(start);
  addEventListener('keydown', e => {{
    const cur = secs.findIndex(s => !s.hidden);
    if (e.key === 'ArrowRight' && cur < secs.length - 1) show(cur + 1);
    if (e.key === 'ArrowLeft' && cur > 0) show(cur - 1);
  }});
</script>
</body>
</html>"""


def borrow_work(snap_user_id) -> "callable":
    """Lend the snapshot account some work, and hand back an undo.

    Work, Review and Approvals are queues — arranged by person, not by object — so
    they are empty for a brand-new account, and an empty queue is the one view that
    tells a designer nothing. This temporarily points a few episodes, one open
    submission and one pending credit request at the snapshot user, purely so those
    screens have something in them. Every field is read before it is written and put
    back afterwards, so the database ends where it started; the two rows created are
    deleted again.
    """
    from flowboard.db import get_session
    from flowboard.db.models import CreditGrant, Scene, Series, Submission
    from sqlmodel import select

    saved: list[tuple] = []
    made_submission = None
    made_grant = None

    with get_session() as s:
        scenes = [
            sc
            for sc in s.exec(select(Scene).order_by(Scene.order_index)).all()
            if sc.assignee_user_id
        ][:3]
        for sc in scenes:
            saved.append((sc.id, sc.assignee_user_id, sc.deliverable_status))
            sc.assignee_user_id = snap_user_id
            s.add(sc)

        # One cut waiting on a verdict, so Review shows its card and player.
        if scenes:
            target = scenes[-1]
            row = Submission(
                scene_id=target.id,
                version=1,
                drive_url="https://drive.google.com/file/d/1SnapDesignPreview000000000/view",
                drive_file_id="1SnapDesignPreview000000000",
                note="Re-graded the night interior and swapped the SFX at 1:12.",
                submitted_by=snap_user_id,
                status="submitted",
                approver_user_id=snap_user_id,
            )
            target.deliverable_status = "submitted"
            s.add(row)
            s.add(target)
            s.commit()
            made_submission = row.id

        # One credit request awaiting a verdict, so Approvals shows its card.
        series = s.exec(select(Series)).first()
        if series is not None:
            grant = CreditGrant(
                scope="series",
                scope_id=series.id,
                amount_usd=250.0,
                reason=(
                    "Two sequences need a re-shoot after the client changed the "
                    "night-market setting — the base budget is spent."
                ),
                granted_by=snap_user_id,
                status="pending",
            )
            s.add(grant)
            s.commit()
            made_grant = grant.id
        else:
            s.commit()

    def restore() -> None:
        with get_session() as s:
            if made_grant is not None:
                g = s.get(CreditGrant, made_grant)
                if g is not None:
                    s.delete(g)
            if made_submission is not None:
                row = s.get(Submission, made_submission)
                if row is not None:
                    s.delete(row)
            for scene_id, assignee, status in saved:
                sc = s.get(Scene, scene_id)
                if sc is not None:
                    sc.assignee_user_id = assignee
                    sc.deliverable_status = status
                    s.add(sc)
            s.commit()
        print("  restored borrowed episodes + removed the temporary submission")

    print(f"  borrowed {len(saved)} episode(s) so Work/Review aren't empty")
    return restore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--front", default="http://localhost:5174")
    ap.add_argument("--api", default="http://localhost:8101")
    ap.add_argument("--user", default="_design_snap")
    ap.add_argument("--password", default="snap-pw-7731")
    args = ap.parse_args()

    if not Path(CHROME).exists():
        sys.exit(f"Google Chrome not found at {CHROME}")

    # A throwaway admin, so the snapshot shows every surface without needing a
    # human's password. Removed again at the end.
    sys.path.insert(0, str(ROOT / "agent"))
    from flowboard.services import user_service

    existing = user_service.get_by_username(args.user)
    made = existing is None
    if made:
        existing = user_service.create_user(args.user, args.password, role="admin")
    restore = borrow_work(existing.id)
    try:
        token = login(args.api, args.user, args.password)
        print(f"signed in as {args.user}\n")
        ids = json.loads(Path("/tmp/snap_ids.json").read_text())
        routes = [
            (path.format(**ids), label, sel) for path, label, sel in SCREENS
        ]
        screens = asyncio.run(snapshot(args.front, token, routes))
    finally:
        restore()
        if made:
            u = user_service.get_by_username(args.user)
            if u:
                try:
                    user_service.delete_user(u.id)
                except Exception:
                    user_service.set_status(u.id, "disabled")

    fav = base64.b64encode(FAVICON.read_bytes()).decode() if FAVICON.exists() else ""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(screens, CSS.read_text(), fav))
    kb = OUT.stat().st_size / 1024
    empty = [s["label"] for s in screens if not s["ready"]]
    print(f"\n→ {OUT}  ({kb:.0f} KB, {len(screens)} screens, self-contained)")
    if empty:
        print(f"  note: never saw content for {', '.join(empty)} — check those by hand")


if __name__ == "__main__":
    main()
