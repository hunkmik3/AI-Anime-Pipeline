# Giantflow refactor — panel production, in the app

Written 2026-08-03. **Scope: `/giantflow` only.** Nothing outside it changes — not
Project → Series → Episode, not the canvas, not the delivery/review flow.

## What this replaces

Today the studio runs a comic-adaptation pipeline across three places:

1. A person cuts the original comic pages into panels → a folder of **raw material**.
2. An artist restyles / extends / redraws each panel in **giantflow**.
3. A PM reviews **on Miro**, one row per panel, writing notes like *"BG bị lệch màu so
   với truyện gốc"*, *"Sai nơ áo"* — and the artist marks each `Fixed`.

Step 1 stays outside the app: the cutter delivers a folder, already in reading order.
**Steps 2 and 3 both move in.** That last point is the one that shapes everything: the
panel board is not a Miro-style tracker bolted on top of giantflow — it *is* where the
artist generates. One place, not a place to look and a place to work.

## The one design argument worth having: grid, not node graph

The brief floated a node-based canvas. I think that is the wrong shape, and the
evidence is the Miro board itself: **what already works for this team is a table** —
one row per panel, one column per stage.

A node graph earns its complexity when work *branches* — this feeds that, a node forks
into variants. Here every panel runs the same straight line (raw → generate → review →
fix → done) and **panels never connect to each other**. Two hundred disconnected nodes
is not a graph; it is a grid you have to pan and zoom to read.

And the daily questions are *which panels are waiting on me*, *which did the PM send
back*, *how many does Quân have left*. A grid answers those by filtering and sorting.
A canvas answers them by hunting.

The real requirement in the brief is the last clause — **see and manage every panel on
one screen**. A dense card grid does that better: one card per panel, raw and result
side by side, status as a border colour, filters across the top. Zoom out for progress,
click in to work.

(Also worth knowing: reusing this repo's existing node canvas is not free — it is bound
to Shot/Scene, which is explicitly out of scope.)

## Data model

`flow_board` becomes a real project, and the panel is the unit of work.

```
flow_project              one comic being adapted  (upgraded flow_board)
  ├─ flow_project_member  user + role, giantflow-only
  └─ flow_panel           PANEL006 — order_index, assignee, status
       ├─ flow_panel_image   role = raw | generated, version, media_id
       └─ flow_panel_note    one PM remark + resolved flag ("Fixed")
```

**Panel status** — `todo → in_progress → submitted → changes_requested → approved`.
`changes_requested` returns it to the artist with the notes attached; resubmitting does
not erase the previous round, so the back-and-forth stays readable (same principle as
the episode submission history).

**`approved` locks generation on that panel.** The work is signed off, so the app stops
accepting new versions for it — enforced server-side, not just by hiding the button, or
the lock is decoration. A PM can *reopen* an approved panel (back to
`changes_requested`), because otherwise one mis-click is unfixable; reopening is audited
like any other verdict.

**Why images get their own table** rather than reusing `reference.source_board_id`:
a panel has *several* raw pieces (PANEL008 in the Miro board has three) and *many*
generated versions across review rounds. Both need an explicit role and ordering, and
"which raw did this result come from" is exactly the comparison every PM note makes.
The current flat `reference` row cannot express it.

**Old giantflow data is dropped** (confirmed): the 13 loose references have no panel to
belong to, and inventing one would be worse than starting clean.

## Roles

Four roles, giantflow-only: **admin · project manager · artist · viewer**.

Reuse `services/permissions.py` — it is already a ranked capability table
(`VIEWER < ARTIST < LEAD < PRODUCER < ADMIN`) with tests, and PM maps onto PRODUCER.
What stays separate is **membership and capabilities**: a `flow_project_member` table
of its own, and a giantflow capability set, so a giantflow role grants nothing in the
production hierarchy and vice versa.

| capability | min role |
|---|---|
| `panel.read` | viewer |
| `panel.generate` | artist — and only on panels assigned to them |
| `panel.submit` | artist |
| `panel.review` | PM (approve / send back with notes) |
| `panel.assign` | PM |
| `flowproject.import` | PM |
| `flowproject.manage` | admin |

The one rule that is not just a rank: **an artist may only generate on panels assigned
to them.** Assignment is by range in the UI (artist 1 → panels 1-30, artist 2 → 31-60),
stored per panel.

## Import

A PM imports the raw-material folder; the app creates exactly that many panels.

- **Order comes from the filename**, ascending. The cutter has already sorted them, so
  the app must not try to be clever — it preserves what it is given.
- **A subfolder is one panel**, and every file inside it is one of that panel's raw
  pieces. A loose file is a panel with a single piece. That is the whole rule, and it
  handles the multi-piece case without a naming convention to memorise.
- Panel code is the file/folder stem (`PANEL006`), shown as-is so it matches the
  cutter's sheet and the Miro history.

## Screens

**`/giantflow`** — project list.

**`/giantflow/:projectId`** — the panel grid. This is the Miro replacement. One card per
panel: raw thumbnail and latest result side by side, status border, assignee, unresolved
note count. Filter by assignee / status / "mine". This is the screen a PM lives on.

**`/giantflow/:projectId/panels/:panelId`** — the workspace, and where generation
happens. Raw material pinned on the left as the reference, version history, the existing
composer bound to *this* panel (raw auto-attached, so "match the original" is the
default), and the note thread on the right with a Fixed checkbox per remark.

The composer, engines and generation path are unchanged — they work. What changes is
what a generation is *attached to*: a panel instead of a loose board.

**The artist keeps full freedom over generation.** No target aspect ratio, model or size
is recorded on the panel and none is imposed: the existing engine and every one of its
controls apply as they do today. The panel supplies context (its raw material as the
reference) and takes custody of the results — it does not constrain how they are made.

## Phases

1. **Schema + import** — the five tables, the folder importer, panels listed in order.
2. **Panel grid** — cards, filters, assignment by range.
3. **Panel workspace** — composer bound to a panel, raw as reference, version history.
4. **Review loop** — submit, approve, send back with notes, resolve notes.
5. **Roles** — giantflow membership + capability gating, enforced server-side.

Each phase ends usable: after 2 a PM can import and assign; after 3 artists can work;
after 4 the Miro board can be retired.

## Export

Approved panels come back out of the app, two ways:

- **One at a time** — download the approved version of a single panel.
- **All approved** — one archive, files named by panel code so the folder arrives in
  panel order and matches the cutter's sheet.

Only approved panels are exported; anything still in review is not a deliverable.

## Notes

A note belongs to **the panel**, not to a version — same as the Miro board today. That
matches how the remarks actually read (*"Sai nơ áo"* is about the panel, and stays true
across re-generations until someone fixes it), and it means a note survives the version
it was written against instead of being orphaned by the next attempt.

## Decisions already settled

Recorded so they are not reopened later:

- **Panel order** comes from the cutter's filenames. The app preserves, never re-sorts.
- **Old giantflow data is dropped** — the loose references have no panel to belong to.
- **Roles reuse `permissions.py`'s machinery** but keep their own membership table and
  capability set, so nothing leaks between giantflow and the production hierarchy.
- **Grid, not node graph** — see the argument above.
