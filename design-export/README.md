# Giant Studio — design export

**One file: `app.html`.** Open it in a browser. A strip along the top switches between
all 14 screens; `←` / `→` step through them, and the screen you're on survives a
reload. No server, no build step, nothing to install — it's self-contained, including
the favicon, so it works from a `file://` path and from a share link.

## Why this matches the app

It isn't a reconstruction. `agent/scripts/snapshot_design.py` starts headless Chrome,
signs in, visits every route, waits for each screen's data to land, and takes **the DOM
the browser actually built** — then inlines the app's real `styles.css` around it. So
the class names are the app's class names and the layout is the app's layout, down to
the wrapping.

An earlier version of this export hand-wrote each screen's markup from the components.
It drifted immediately and didn't match. That version is gone; don't reintroduce it.

**Real data, from the demo database.** A 36-character series title, `$1056.25` total,
`46.5%` re-rolls, an unstaffed row sitting among staffed ones. The layout problems worth
fixing are the ones that only show up at real length, and placeholder text hides exactly
those.

## The 14 screens

| # | Screen |
|---|---|
| 1 | Projects |
| 2 | Project home — series, each with its episode cards |
| 3 | Series — producer, budget, progress, episodes |
| 4 | Episode — assignee, quota, delivery, sequences |
| 5 | Work — what an employee owes, grouped by whose move it is |
| 6 | Review — cuts awaiting a verdict, with the player and the two decisions |
| 7 | Admin · Members |
| 8 | Admin · Approvals — pending credit requests |
| 9 | Admin · Spend summary — pool, credits over time, credits by person |
| 10 | Admin · Delivery tracker |
| 11 | Admin · Cost tree — project → series → episode → sequence |
| 12 | Admin · Spend ledger — every billed generation |
| 13 | Admin · Series & episodes, with the tier colours |
| 14 | Admin · Audit log |

**The canvas is not included.** That surface is out of scope for redesign, and it needs
React to render anything meaningful.

## Regenerating

Needs the app running (frontend + backend) and Google Chrome installed:

    cd agent
    FLOWBOARD_DATABASE_URL='postgresql+psycopg://flowboard:flowboard@localhost:15432/flowboard_demo' \
      .venv/bin/python scripts/snapshot_design.py

Two things it does to the database, both undone before it exits:

- Creates a throwaway admin (`_design_snap`) so it doesn't need a real password.
- **Borrows** 3 episodes, one open submission and one pending credit request, so Work,
  Review and Approvals aren't captured empty — an empty queue tells a designer nothing.
  Every field is read before it's written and put back afterwards; the two rows it
  creates are deleted again. Verified by fingerprinting `scene`, `submission` and
  `credit_grant` either side of a run.

## Porting a redesign back

`app.html` is generated — **don't hand-edit it expecting the app to change.**

- **Visual changes** (colour, spacing, type, borders, radii) → `frontend/src/styles.css`.
  That file is the one embedded here, so a rule you change lands in the app directly.
- **Layout changes** needing different markup → the React component. The class names
  are the map: `.tl__row` is in `components/admin/SpendOverTime.tsx`, `.inbox__facts` in
  `components/DeliveryCard.tsx`, `.ep__facts` in `routes/EpisodePage.tsx`.
- **New tokens** → the `:root` block at the top of `styles.css`. Dark theme, emerald
  accent (`--accent: #00a76f`); components read the tokens, so a re-skin starts there.

Two things were deliberate, so decide before changing them:

- **Tier colours** (`.crm-chip--tier-*`) match the studio's own spreadsheet — S orange,
  A magenta, B cyan, C green, D grey. They look loud on purpose: the team checks them
  against the sheet by eye.
- **Bars keep a 3px minimum fill.** Spend runs from a $220 day to a $0.75 one, so
  without a floor most days round to invisible. The scale itself stays linear — a busy
  day really is 300× a quiet one.

## Known rough edges

Recorded so a redesign doesn't have to rediscover them:

- **57 button classes** (`.btn`, `.btn2`, and both `.btn--sm` *and* `.btn--small`).
  There is no single button. Consolidating them is the biggest win available.
- **7 table implementations** (`.rtable`, `.admin-table`, `.admin2`, `.pshots`,
  `.drill`, …). Only `.rtable` is used by the newer screens.
- **73 distinct font sizes**. There is no type scale.
- `BudgetPanel` and `ApprovalsTab` still carry private inline styles, so they won't
  respond to stylesheet changes until converted.
