# UI/UX plan — arrangement

The owner's assessment: *"cách sắp xếp trang rất rối… nó đang được chắp vá khá nhiều và
không cái nào ra cái nào cả."*

This is not about styling. It is about **what lives on which page**.

## The diagnosis

The app is arranged **by feature**. The four screens the owner is happy with are
arranged **by object**. That single inconsistency produces everything that feels
patched together.

### Arranged by object — the parts that work

    Projects  →  MoguTV  →  OUTF  →  (canvas)

Each page is *one thing*, showing its children as cards, drilling down one level at
a time. The sidebar mirrors it as a tree. Nothing to learn: the page you are on is
the thing you are looking at.

### Arranged by feature — the parts that don't

`Staffing`, `Cost breakdown`, `Tracker`, `Series & episodes` are each *one
capability applied across every object*. So a single episode's information is
spread over seven places:

| To see or change | You go to |
|---|---|
| Name, code, sequences | project home (cards) |
| Assignee, quota | `/manage/:id` → Staffing (table) |
| Cost | `/admin` → Cost breakdown (tree table) |
| Delivery state | `/admin` → Tracker **or** `/manage` Staffing **or** `/review` |
| Submission history | `/review` **or** `/work` |
| Change history | `/manage/:id` → History button |
| Tier, status, priority | `/admin` → Series & episodes (CRM table + modal) |

And the same Project → Series → Episode tree is drawn **five different ways**:
cards with drill-down, a nested expanding list, a table whose rows expand into
another table, one flat table merging every series of every project, and a sidebar
tree. Two of those the owner likes. The other three exist because the data had
nowhere else to go.

### The missing page

There is **no page for an episode**. You go from the project home straight into the
canvas. So everything *about* an episode — who owns it, what it may spend, what it
cost, whether it was delivered, what came back and why — had to be put somewhere
else, and ended up scattered across `manage` and `admin`.

That one gap is the origin of most of the mess. Fill it and the feature-shaped
pages have no reason to exist.

## The principle

> **One object, one page. Features are sections on it, not pages of their own.**

A page answers "what is this thing?" — not "who is assigned to everything?".

Two exceptions, both legitimate:

- **Inboxes** (`/work`, `/review`) are arranged by *person*, because "what do I have
  to do" is a real question that no object page can answer.
- **Admin** keeps only what genuinely isn't about one object: accounts, the approval
  queue, the audit log, and company-wide rollups.

## The structure

    /projects                          every project — cards            KEEP
    /projects/:p                       one project — series cards       KEEP
    /projects/:p/series/:s             one series — episode cards       NEW
    /projects/:p/episodes/:e           one episode — everything         NEW ← keystone
    /projects/:p/episodes/:e/canvas    the canvas                       KEEP

    /work                              mine to deliver                  rebuild
    /review                            mine to judge                    rebuild

    /admin/members                     accounts + roles                 keep, restyle
    /admin/approvals                   signups + credit requests        keep, restyle
    /admin/spend                       company-wide money               merge 2 tabs
    /admin/audit                       security log                     keep, restyle

    /manage/*                          DELETE — its job moves onto the object pages
    /admin/projects                    DELETE — that is /projects
    /admin/series-episodes             DELETE — that is the series page
    /admin/tracker                     → /admin/spend (company rollup only)

### What each new page holds

**Series page** — everything the CRM table held for one series, in place:
identity (name, code, tier, genres), the people (producer), the money (budget vs
spend), progress, and its episodes as cards. Editing a field happens here, not in a
modal on a different page.

**Episode page** — the keystone. One page holding what is currently in seven:

    ┌ EP007 · She Wears Special Outfit For Boss ─────── [Open canvas →] ┐
    │ Assignee ▾   Quota $40 · $12.80 used   Status: sent back          │
    ├───────────────────────────────────────────────────────────────────┤
    │ Sequences        SQ01 SQ02 SQ03 …            (cards, → canvas)    │
    │ Delivery         v2 approved · v1 sent back "grade too warm"       │
    │ Spend            $12.80 across 24 generations, 3 retakes           │
    │ History          who changed what, when                            │
    └───────────────────────────────────────────────────────────────────┘

Everything about the episode, where the episode is. No hunting.

## Phases

Ordered so the app is never half-broken: build the new object pages first, and only
delete a feature page once its job has a home.

### Phase 1 — The episode page

The keystone. Nothing else can be simplified until this exists. Assemble it from
components that already work (`PersonPicker`, `QuotaField`, `HistoryDrawer`,
submission list) — this is arrangement, not new capability.

### Phase 2 — The series page

Same move one level up. Absorbs the CRM fields so `/admin → Series & episodes` has
nothing left that isn't a cross-project rollup.

### Phase 3 — Delete `/manage`

Once Phases 1–2 land, `/manage` holds nothing unique: structure editing belongs on
the project and series pages, staffing and quota on the episode page, exports on
whichever object they describe. Deleting it removes a whole parallel navigation.

### Phase 4 — Shrink admin to four

`Members · Approvals · Spend · Audit`. `Projects` and `Series & episodes` go
(duplicates of the object pages); `Spend overview`, `Cost breakdown` and `Tracker`
merge into one company-wide money page — they are three views of one question.

### Phase 5 — Rebuild the two inboxes

`/work` and `/review` are correctly arranged (by person) but were built as one-off
pages with their own private styling. Rebuild them on the shared page skeleton, and
have every row link to its episode page rather than repeating the episode's details
inline.

### Phase 6 — One page skeleton, one set of primitives

Only now, with the arrangement settled, is it worth unifying the surface. The
measured state to fix: **57 button classes** (including both `.btn--sm` and
`.btn--small`), **7 table implementations**, **73 distinct font sizes**, and two
incompatible styling approaches where which one a page uses depends on when it was
written.

Direction: formalise what the pages the owner likes already do — those pages define
the design language; the others simply don't follow it. No new look is being
invented.

## What is not touched

Per the owner's screenshots: the **canvas**, the **projects list**, the **project
home**, and the **sidebar tree**. These are the reference for everything else, not
candidates for change.

## Sequencing note

Phases 1–3 are the ones that address "rối" — they cut seven scattered locations
down to one per object and remove a duplicate navigation. Phases 4–6 are cleanup
that gets cheaper after them, not before.
