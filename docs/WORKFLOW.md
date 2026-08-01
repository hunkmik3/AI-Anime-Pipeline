# The studio workflow — source of truth

The owner supplied five diagrams describing how the studio should run. This file
records them, what is built against each, and the decisions taken along the way —
so none of it has to be re-derived or re-argued next session.

Companion: [UX_PLAN.md](UX_PLAN.md) (how the screens are arranged and why).

> **Note on `PLAN.md`:** that file's Post-MVP phases (8–12) describe a different,
> abandoned roadmap and no longer match reality. Treat this file and `UX_PLAN.md` as
> current.

---

## What the app is for

Turn AI video generation from an uncontrolled cost centre into a managed production
line. Three things the system enforces rather than trusting people to remember:

1. **Nobody overspends.** Hard ceilings, and going past one needs an approved,
   logged, reasoned decision.
2. **Every piece of work has one owner and one reviewer.** Nobody reviews their own
   work; nothing sits unreviewed because "who was supposed to look at this?".
3. **Everything leaves a record.** The studio ran on Google Sheets + Drive +
   Discord, and the problem it came to solve was that *nothing left a trace*.

---

## A → Z, as it works now

    BOD                                         → /admin › Projects
      creates the project, names the PM as owner
      sets the project credit ceiling

    PM                                          → project page
      creates a Series
      creates Episodes (single or bulk)

    PM                                          → series page
      names the Series Producer  (first reviewer in the chain)
      sets the series ceiling

    PM                                          → episode page
      assigns the Employee       (ONLY this person may hand it in)
      sets the episode quota

    Employee                                    → episode page › Sequences, then canvas
      splits the episode into Sequences  (hard-capped at planned ±2)
      generates video on the canvas      (each generation checked against
                                          sequence → episode → series → project;
                                          the innermost breached ceiling blocks)

    Employee                                    → /work
      edits the cut outside the app, uploads to Drive
      hands in the Drive link + a note

    Series Producer                             → /review
      watches the cut IN THE APP (file stays Restricted on Drive)
      approves  → episode locked
      sends back → returns to Draft, quota NOT refunded, reason required

    BOD                                         → /admin › Spend & delivery
      Summary · Delivery (tracker) · By project · Ledger

### Navigation

| Surface | Who | What |
|---|---|---|
| `/projects` | everyone | projects → series → episodes → canvas |
| `/projects/:p` | everyone | one project: series as cards |
| `/projects/:p/series/:s` | everyone with access | one series: episodes, fields, budget, history |
| `/projects/:p/episodes/:e` | assignee + above | one episode: **everything** about it |
| `/projects/:p/scenes/:e` | assignee + above | the canvas (full bleed) |
| `/work` | everyone | what I must hand in |
| `/review` | reviewers | what waits on my verdict |
| `/admin` | admin/BOD only | Members · Approvals · Spend & delivery · Projects · Series & episodes · Audit |

---

## Diagram-by-diagram status

### Diagram 1 — Project → submission, end to end

| Step | Status | Where |
|---|---|---|
| BOD creates project, sets PM + owner | ✅ | `/admin › Projects` |
| PM creates Series | ✅ | project page |
| PM assigns Series Producer | ✅ | series page |
| BOD sets credit budget | ✅ | project & series pages, `/admin` |
| PM creates Episode | ✅ | project page |
| PM sets episode quota | ✅ | episode page |
| PM assigns Employee | ✅ | episode page |
| PM "creates KPI" | ⚠️ **derived, not created** — see below |
| Employee splits into Sequences | ✅ | episode page + canvas, capped ±2 |
| Employee generates video, uses credits | ✅ | canvas; blocked at the tightest ceiling |
| Employee pastes Drive link, submits | ✅ | `/work` |
| Series Producer reviews → accept/reject | ✅ | `/review` |
| rejected → back to generate | ✅ | returns to Draft |
| accepted → **Payroll** | ❌ **out of scope** — owner's decision |

### Diagram 2 — Deliverable lifecycle

`Draft → Submitted → Approved → Paid`, with `Rejected → Draft`.

| Rule | Status |
|---|---|
| Draft: owner edits freely | ✅ |
| Draft → Submitted | ✅ |
| Submitted → Approved | ✅ |
| Submitted → Rejected → **back to Draft** | ✅ |
| **Rejection does NOT refund quota** | ✅ deliberate; the cost of a re-roll stays with the work |
| Reject requires a reason | ✅ |
| Approved locks production | ✅ resubmission refused |
| **Only one attempt open at a time** | ✅ added after the data showed v2–v5 all "awaiting review" |
| Nobody reviews their own submission | ✅ |
| `Approved → Paid` | ❌ needs Payroll (out of scope) |
| `Paid` = immutable | ⚠️ state declared and blocks resubmission; nothing transitions into it |

### Diagram 3 — Finding the approver

    Series Producer → project PM → escalation owner,
    skipping anyone who is the submitter

✅ **Fully built** — `submission_service.resolve_approver`. Stricter than the
diagram in two ways: it also skips deactivated accounts, and returns `None` as a
*configuration error* rather than letting work sit unreviewable.

⚠️ The escalation owner is only settable by hand (`project.settings
["escalation_owner_id"]`); with none set it falls back to the first admin.

### Diagram 4 — Visibility scope

Assigned at a node → you see its ancestors (to navigate), the node, and everything
beneath it. **Never your siblings.**

✅ Built, derived from the assignments a PM already makes (`scene.assignee_user_id`,
`series.producer_user_id`) rather than a second place to declare access — so handing
someone an episode grants exactly the access to do it, and taking it back removes it.

- Narrowing applies to **artist** and **viewer** only. Producers and leads build the
  structure others get assigned to, so scoping them to their own assignments would
  stop them working.
- Out-of-scope reads **404, never 403** — a 403 confirms the id exists, which is what
  an enumeration attack wants.
- ⚠️ **Behaviour change:** an artist with no assignment sees nothing inside a
  project. This is intended (access follows assignment) but surprises people, so a
  PM must assign before an artist can start.

### Diagram 5 — Generation quota loop

| Step | Status |
|---|---|
| Does this **Sequence** have quota left? | ✅ all four tiers carry a ceiling |
| Yes → call Seedance, deduct, log | ✅ |
| No → **block** (not warn) | ✅ refused before anything is queued |
| Alert **Employee + Series Producer + PM** | ✅ all three |
| PM requests more, reason mandatory | ✅ |
| Approved grant raises the ceiling | ✅ **an admin approves** — see decisions |
| Log the grant with its reason | ✅ |
| No → hand in the best version already generated | ✅ |

**Tiers are independent — an Episode quota is NOT divided among its Sequences.**
Confirmed by the owner: option A, one shared pot, first-come-first-served.

---

## Decisions taken (do not re-litigate)

| Decision | Why |
|---|---|
| **Payroll is out of the app** | Owner's call. `Paid` state stays declared for later. |
| **KPI is derived, never entered** | The diagram says "create KPI" but never says what it measures. Rather than invent a pass mark that people would be judged by, the app computes what the record supports: delivered, attempts, sent-back, first-pass rate, credits/episode, review days. **No scores, no targets.** Owner confirmed it is a tracker for visibility, not pay. |
| **Nothing about hours or speed** | The data records when work was *assigned* and *submitted*, never when someone started. A productivity figure would be fabricated. |
| **KPI/tracker is admin + BOD only** | Owner's call. Deliberately not shown to a PM: comparing colleagues' output is not information the app hands to a peer, even a senior one. |
| **Episode quota not split across sequences** | Owner chose option A. Splitting evenly would be wrong — a fight scene costs several times a dialogue scene. |
| **Grants need an admin, not the PM** | Diagram 5 shows the PM granting; the owner overrode this. If it ever needs reverting, that is the one place the build departs from the diagrams. |
| **Only one submission open per episode** | The lifecycle leaves `Submitted` only via the approver, so stacking attempts produced several "awaiting review" rows for one episode with nothing to say which counted. |
| **Roster edits never move ownership** | Dropping the owner from the roster used to promote whoever was first — an artist could end up holding producer rights. Ownership moves only through the admin-only project update. |
| **404 not 403 for anything scoped** | A 403 confirms an id exists. |

---

## Open questions for the owner

1. **Escalation owner** — currently settable only by editing project settings by
   hand. Worth a UI, or is falling back to an admin acceptable?
2. **Admin `Projects` / `Series & episodes` tabs** — kept because they hold
   admin-only provisioning (create/delete a project, assign an owner) and the
   cross-project comparison table. Rename `Projects` → `Provisioning` and trim the
   parts that duplicate the object pages?
3. **Payroll hand-off** — with payroll outside the app, which columns does the
   external process need from the CSV export?

---

## Known gaps / debt

- **Node and edge routes still ungated at the object level** was fixed; the wider
  authorisation sweep closed 42 endpoints. `tests/test_route_authorization.py`
  enumerates every live route and fails when a new one authorises nothing — that is
  the regression guard, keep it green.
- **48 pre-existing test failures** in `test_video_provider_*` / `test_video_registry`
  / `test_requests` / `test_phase8_*`. Verified present at HEAD before any of this
  work — not caused by it, but still debt.
- **Two components still carry private inline styles** (`BudgetPanel`,
  `ApprovalsTab`); they are next in line for rearrangement, so converting their
  styling now would mean doing it twice.
- **`styles.css` is ~10.5k lines** with 57 button classes and 7 table
  implementations. A `.crm-form` block was missing its closing brace, which silently
  broke every rule after it — that was the real cause of the "CSS never loads"
  problem that led to inline styles everywhere. Fixed; the balance is worth checking
  after any large CSS edit.
- **Uncommitted.** Everything from Phase 10 onward is still working-tree only.
  Migrations to run on deploy: `d6e7f8a9b0c1` (audit object ref), `e7f8a9b0c1d2`
  (shot production bag), plus the earlier `a3b4c5d6e7f8` / `b4c5d6e7f8a9` /
  `c5d6e7f8a9b0`.

---

## Demo data

`agent/scripts/seed_demo_activity.py` fills a **demo** database with believable
activity so these screens can be judged with numbers in them.

    python scripts/seed_demo_activity.py --dry-run   # look first
    python scripts/seed_demo_activity.py
    python scripts/seed_demo_activity.py --undo
    python scripts/seed_demo_activity.py --rebudget  # ceilings from measured spend

Refuses to run against `flowboard_server` (production) or any database whose name
doesn't look like demo/test. Writes are tagged so `--undo` removes exactly them.

It models retakes the way the app does — several takes per shot slot, last one kept —
because the waste figure is computed per node as "last take kept, earlier wasted".
One take per node reports 0% waste, which reads as "nothing wasted" when it really
means "nothing to compare".


---

## Setting up a second machine (review box, deploy)

Two things do not travel with the repo, and both look like "the app is broken"
rather than "something is unconfigured".

### 1. Install from requirements.txt, don't rely on the dev machine's venv

`pillow` was missing from `requirements.txt` until 2026-08-01 while being imported
by `routes/media.py`. The dev machine happened to have it installed by hand, so
thumbnails worked there and **every `/thumb` returned 500 on a fresh venv** — which
presents as a blank review page (no thumbnails anywhere), not as a missing package.
`anyio` and `typing_extensions` were in the same state, arriving only transitively
via starlette and pydantic while being imported directly.

    cd agent && .venv/bin/pip install -r requirements.txt

### 2. Google Drive credentials — two files, neither in git

Reviewers watch the submitted cut without a Google account: the app holds its own
Drive identity and proxies the bytes, so files stay `Restricted`. That needs two
files in `agent/` (both gitignored, paths overridable with `FLOWBOARD_DRIVE_CLIENT`
/ `FLOWBOARD_DRIVE_TOKEN`):

| File | What it is |
|---|---|
| `oauth-client.json` | the Desktop-app OAuth client from Google Cloud |
| `drive-token.json` | the refresh token, written by `scripts/drive_auth.py` |

Without them the video player 401s and the rest of the review page still works —
list, assign, approve, reject — so a review box missing only these is usable for
everything except watching the cut.

**Fastest path for a new machine:** copy both files from a machine that already
works. The refresh token is not machine-bound.

**If you want that machine to hold its own token** (revocable on its own), copy
only `oauth-client.json` and run:

    agent/.venv/bin/python agent/scripts/drive_auth.py

Sign in as the **robot account** — whichever account you pick becomes the identity
that reads submitted cuts, so it must be the one the submissions folder is shared
with. Scope is read-only: the app can never modify or delete studio files.
