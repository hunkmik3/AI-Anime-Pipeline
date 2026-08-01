# Integration plan — Flow Studio (giantflow) → Giant Studio

Written 2026-07-31. Source: `/Users/mac/manga_extract/manga_extract` @ `1385d21`,
target: this repo on `review/phase10-crm`.

> **Status: the bring-over is DONE and running at `/giantflow`, open to the whole
> team.** What shipped is
> §8; the integration phases in §5 are still ahead and unchanged. The studio is
> deliberately standalone — its own board list, no tie to Project → Series →
> Episode, no per-project RBAC, no credit budget. Do not treat §5 Phase 1 as done:
> the engines run through the studio's own worker handler, **not** through
> `services/image/registry.py`, and nothing writes `UsageRecord` yet.

**Scope: Flow Studio only.** The comic node pack (`services/comic/panels.py`,
`panel_ml.py`, `characters.py`, `magi.py`, `sheet.py`, `routes/comic.py`, the 7
`Comic*Body.tsx` nodes, `PanelBoxEditor.tsx`) is **out of scope and will not be ported**.
That also drops the two heaviest risks from the earlier draft — the 10.8 GB of panel
intermediates and the optional torch/YOLO/CCIP stack. Neither is needed.

---

## 1. What Flow Studio actually is

A standalone image-generation studio: a prompt bar with `@mention` tokens for characters
and scenes, a composer, a results grid, three interchangeable engines, and a
quota/cost readout. Roughly **3.6k lines**, and it is far more compatible with this repo
than the directory names suggest.

| Piece | Lines | Where it goes |
|---|---|---|
| `frontend/src/flow/FlowViewer.tsx` | 722 | `frontend/src/flow/` as-is |
| `store/flowStudio.ts` | 661 | `frontend/src/store/` as-is |
| `frontend/src/flow/FlowComposer.tsx` | 523 | as-is |
| `frontend/src/flow/FlowApp.tsx` | 481 | becomes a project-scoped route |
| `services/comic/atrium_api.py` | 288 | → `services/image/atrium.py` |
| `services/comic/ark_api.py` | 260 | → `services/image/ark.py` (Seedream 5.0 Pro) |
| `services/comic/gemini_api.py` | 254 | → `services/image/gemini.py` (Nano Banana) |
| `worker/processor.py::_handle_flow_gen_image` | ~90 | folds into A's existing `_handle_gen_image` |
| `frontend/src/flow/FlowPromptBar.tsx` | 117 | as-is |
| `store/flowProjects.ts` | 111 | **drop** — A has `ProjectFlowMapping` |
| `routes/flow_usage.py` | 125 | **drop** — A's UsageRecord + ledger is better |
| `lib/storyboardPrompt.ts` | 95 | as-is |
| `routes/flow_projects.py` | 168 | **drop** |
| `store/appMode.ts` | 61 | **drop** — A has real routing |

---

## 2. Why this is a small job: A already has every socket

Three things I expected to be the hard parts turned out to already exist here.

**The three engines are self-contained.** `gemini_api`, `atrium_api` and `ark_api` import
nothing but stdlib + `httpx`. No coupling to the comic services, no shared state, no
bridge. They drop in behind A's existing `ImageProvider` Protocol
(`services/image/base.py`) and register in `services/image/registry.py`. **The dependency
delta for the whole integration is zero new packages.**

**A's `Reference` model is already the right shape.** Flow Studio's `FlowAsset` needs
`mediaId, label, prompt, aspectRatio, tags[], pinned, createdAt` — and A's `Reference`
carries `media_id, label, ai_brief, aspect_ratio, tags (JSONB list), pinned, position,
project_id`. The `@mention` system is just tag prefixes (`char:`, `scene:`, `ref:`) over
that list. **So Flow Studio's character/scene library and A's per-project Asset Library
are the same thing** — the mentions become project assets automatically, reusable in any
Image or Video node.

**A's request/worker pipeline is identical.** Flow Studio calls `createRequest` then polls
`getRequest`; A has the same `Request` table and a handler table in
`worker/processor.py` that already contains `gen_image` and `edit_image`. So
`flow_gen_image` doesn't need a new request type — it collapses into A's `gen_image` with
a `model_id`, and the polling UI works unchanged.

**No Chrome bridge needed.** All three engines are HTTP APIs (Atrium gateway, Google
Gemini, BytePlus Ark). The single-user browser-session conflict from the earlier draft
does not apply to this scope at all.

---

## 3. What has to be built rather than copied

Four items. Everything else is a move.

### 3.1 Provider adapters (small)
Each engine exposes its own function shape with a progress `Callable`. Wrap each in a
class satisfying `ImageProvider` (`submit`, `is_available`, `capabilities`) and register:

| model_id | engine | notes |
|---|---|---|
| `gemini-nano-banana` | gemini_api | daily quota, not per-image billing |
| `atrium-nano-banana` | atrium_api | Atrium gateway; quota ≈ 1000/day |
| `seedream-5-0-pro` | ark_api | **pay per image** — $0.045 out, $0.003/ref, first ref free |

### 3.2 Money must land in the ledger (the important one)
B counted images in a private endpoint (`flow_usage`); nothing reached a budget. Here,
each generation must write **`UsageRecord`** and check **`scope_budget`** before dispatch,
exactly like video does. Two consequences worth stating:

- Seedream spend shows up in the same **Spend ledger / Cost tree / tracker** as video, so
  pre-production money stops being invisible to the BOD.
- The 4-tier budget can actually **block** an overspending project.

Gemini/Atrium are quota-based not dollar-based, so they need a **quota** counter alongside
the dollar one — port `flow_usage.py`'s day-boundary logic into `stats_service`, not as a
separate route.

### 3.3 Authorization on every new endpoint
Flow Studio in B has no users and no permission checks. Here every route it needs must go
through `resource_guard.authorize_project`. `test_route_authorization.py` enumerates
`app.routes` and **fails the build** on any `/api` route lacking an authz marker, so a
missed gate can't ship silently.

### 3.4 Two small dependencies to sever
- `store/flowStudio.ts` calls `uploadComicSheet` — repoint at A's `uploadImage`.
- `FlowApp.tsx` imports `appMode` (`FLOW_ONLY`) and `flowProjects` — both replaced by A's
  router and `ProjectFlowMapping`.

---

## 4. Where it lives: `/giantflow`, mounted whole

**Decided: one top-level route `/giantflow` carrying Flow Studio's own UI unchanged.**
Not a restyle, not a re-layout inside A's page shell — a mount.

This is affordable because the UI is already isolated, which I verified rather than
assumed:

- **127 of the 134 classes it uses are namespaced** — `fc__*` (composer), `fv__*`
  (viewer), `fn__*` (nav/usage), `flow*`. The other 7 are JS fragments from the
  extraction, not classes.
- **A defines zero `.fc/.fv/.fn/.flow` rules** → no collision in either direction.
- **Every CSS custom property B uses already exists in A's `:root`** → colours and
  spacing resolve against A's theme with no new tokens.
- **Zero dependency delta.** B needs no package A lacks.

So: 197 CSS rules move into their own `frontend/src/flowstudio.css`, imported once by the
route. A's 11.3k-line `styles.css` is not touched — which also contains the blast radius
of a malformed rule (a missing `}` on `.crm-form` once swallowed every rule after it).

### The shell is the only part that changes

| B's shell | Replaced by |
|---|---|
| `store/appMode.ts` (`FLOW_ONLY` mode switch) | `<Route path="/giantflow">` inside A's protected layout — auth comes free |
| `store/flowProjects.ts` | B's own project switcher (`fn__projects`, `fn__newproj`, `fn__prow-*`) rewired to A's `Project` |
| `getFlowUsage()` | A's `UsageRecord` + `scope_budget` |
| `uploadComicSheet()` | A's `uploadImage()` |

### Which project does a `/giantflow` generation bill to?

`/giantflow` is global, but budget, RBAC and References in A are all per-project — so this
has to have an answer. **B's UI already has the affordance**: it ships its own project
list. Wire that switcher to A's projects, and the rest resolves itself — the selected
project scopes the mention library, receives the `UsageRecord`, and supplies the RBAC
check. Nothing in the interface is lost, and nothing escapes A's money/permission model.

---

## 5. Phases

**Phase 1 — engines as providers** (~1–2 days) ← *ship this even if the rest waits*
Move the three engine files to `services/image/`, write the adapters, register them, wire
`UsageRecord` + `scope_budget`, extend `_handle_gen_image` to route by `model_id`.
Ship point: **A's existing Image node generates through Nano Banana and Seedream 5.0
Pro**, billed, budgeted, visible in the ledger. No new UI. Useful on its own.

**Phase 2 — quota + cost reporting** (~half a day)
Fold `flow_usage`'s day-boundary quota logic into `stats_service`; surface today's
quota-used and Seedream dollars in the existing tracker. Ship point: the tracker tells the
truth about image spend.

**Phase 3 — the studio surface** (~2 days)
Port `flow/` (4 files) + `store/flowStudio.ts` under `/projects/:id/studio`, gated by
`resource_guard`. Repoint `uploadComicSheet` → `uploadImage`; delete `appMode`/
`flowProjects` usage. Restyle to A's tokens (dark, `--accent #00a76f`) rather than B's
own CSS. Ship point: the studio runs inside the app, with login and permissions.

**Phase 4 — mentions become Asset Library** (~1 day)
Map `char:` / `scene:` / `ref:` tag prefixes onto A's per-project References so a
character created in the studio is pickable from any Image/Video node, and vice versa.
Ship point: consistency across episodes actually works — this is the payoff.

**Phase 5 — retire the duplicate** (~half a day)
Stop B's uvicorn (it squats port 8101 and has caused 404s against this API). Keep the
comic board running separately if still wanted, or archive. Fold the giantflow parts of
`COMIC_PIPELINE.md` into `docs/WORKFLOW.md`.

Rough total: **5–6 working days**, front-loaded.

---

## 6. Decisions needed before Phase 1

1. **Which engines matter?** All three, or just Atrium (Nano Banana) + Seedream? Memory
   says Atrium is live-verified and Seedream routes through Avis for video already — so a
   Gemini-direct path may be redundant. Dropping one saves ~half a day.
2. **Project-scoped `/projects/:id/studio` as recommended**, or do you want the studio
   reachable globally the way it is in giantflow today?
3. **Does the studio also need to write to a canvas?** In B it's a standalone grid. If
   approved images should land on the sequence canvas as `visual_asset` nodes, that's a
   small addition to Phase 4 — but say so now, it changes the data flow.

---

## 7. Out of scope, recorded so it isn't re-litigated

The comic pipeline stays in `manga_extract`: panel detection, text removal, 9:16
extension, 2×2 storyboard stitching, the character DB, the YOLO/CCIP ML extras, and the
10.8 GB of intermediates. If it's wanted later, the earlier draft's analysis still holds —
it would go in as a node pack on the sequence canvas, and Phase 1 here is a prerequisite
either way.

---

## 8. What the bring-over actually shipped (2026-07-31)

The studio runs at **`/giantflow`**, admin-only, with its own UI unchanged. Verified
in a real browser: renders, the board list loads, the quota meters tick, no JS
errors, no page overflow.

### Files added

| Path | What |
|---|---|
| `agent/flowboard/services/flowstudio/{gemini,atrium,ark}_api.py` | the three engines, byte-identical to source except one import line |
| `agent/flowboard/services/flowstudio/errors.py` | `BridgeEditError`, defined locally so `comic/bridge.py` (371 lines of browser bridge) stays behind |
| `agent/flowboard/services/flowstudio/r2.py` | public-URL uploads for Atrium inputs; boto3 lazily |
| `agent/flowboard/worker/flowstudio.py` | the `flow_gen_image` handler + 3 engine adapters + LAB colour matching, lifted from `processor.py` lines 2572-2933 |
| `agent/flowboard/routes/flowstudio.py` | boards CRUD, image upload, usage meter — all `require_unscoped` |
| `agent/alembic/versions/f8a9b0c1d2e3_flow_studio_board.py` | `flow_board` table + `reference.source_board_id` |
| `frontend/src/flow/{FlowApp,FlowComposer,FlowViewer}.tsx` | the surface, unchanged apart from the shell |
| `frontend/src/store/{flowStudio,flowProjects}.ts` | its state |
| `frontend/src/flowstudio.css` | 197 selectors, extracted; loaded only on the route |

### Files edited

`db/models.py` (+`FlowBoard`, +`Reference.source_board_id`), `worker/processor.py`
(+`_ingest_png(s)`, registers the handler), `routes/references.py` (accepts and
filters `source_board_id`), `main.py`, `api/client.ts`, `App.tsx`,
`components/shell/TopBar.tsx`.

### Deliberately NOT brought over

- The whole comic pipeline — panels, text removal, 2×2 stitching, character DB, the
  torch/YOLO/CCIP extras, 10.8 GB of intermediates.
- `routes/boards.py`, `flow_projects.py`, `flow_usage.py`, `Board`,
  `BoardFlowProject`, `MediaProjectMapping` — this repo has better equivalents or
  no need.
- `store/appMode.ts` — replaced by the real router.
- `flow/FlowPromptBar.tsx` and `lib/storyboardPrompt.ts` — the first is dead in the
  source repo (nothing imports it, and its CSS classes don't exist); the second is
  used only by the comic canvas.

### Four defects found and fixed during the port

1. **Two "Project 1" boards on first open.** React StrictMode double-invokes
   effects in dev, so both `load()` calls raced past the empty-list check and each
   created a starter board — 31 ms apart. Guarded in the store, not the effect, so
   every caller is covered.
2. **Composer clipped off the bottom of the screen.** The shell asks for `100vh`,
   but here it sits below a 52 px top bar. It is now a flex child that takes what's
   left (`.app-main > .flow-app`).
3. **Broken logo.** `/symbol.png` shipped with the source repo's `public/`. Points
   at the app's own `favicon.png` now — one mark across the whole app.
4. **Placeholder wrapped to two lines in a one-line box**, so the second line was
   clipped. Copy shortened; `white-space: nowrap` was not an option because it
   would break the textarea's auto-grow measurement.

### Verification

- `tests/test_route_authorization.py`: **163 passed**, up from 157 — the 6 new
  endpoints were each detected as gated. A missed gate fails the build.
- Full suite: **48 failed, 946 passed** — the same 48 pre-existing failures in the
  same files (`test_video_provider_*`, `test_video_registry`, `test_requests`,
  `test_phase8_*`, `test_processor_tier_fallback`, `test_video_models_route`). No
  new failures.
- Two `test_stats_cost_waste` failures were fixed on the way: they still asserted
  the old 3-tier cost tree from before the series tier was added. Stale tests, not
  a regression — unrelated to this port.
- `npx tsc --noEmit`: clean.

### Two things to know before using it

- **Keys are configured and reference generation is verified end-to-end**
  (2026-07-31): a non-admin user generated a 16:9 image from a reference through
  Atrium in 21 s. Two things had to be fixed to get there, both worth knowing:

  **`numpy` + `opencv-python-headless` were missing.** `services/flowstudio/r2.py`
  re-encodes an input image before pushing it to R2, so without `cv2` it returned
  `None` and Atrium failed with `atrium_needs_public_url` — an error that reads like
  missing configuration and isn't. Both are now in `requirements.txt`.

  **Flow Studio's R2 is namespaced `FLOWSTUDIO_R2_*`.** This app already stores its
  own media in R2 via `services/llm/secrets.py`, using `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY` and `R2_BUCKET` — the same three names. The studio needs a
  *different* bucket (a separate account, because it must have public `r2.dev`
  access and the app's bucket is private), so setting the bare names would have
  silently repointed the app's media storage at the wrong bucket.
  `flowstudio/r2.py` reads `FLOWSTUDIO_<NAME>` first, bare `<NAME>` second, so a
  single-bucket install still needs no extra config.

  One diagnostic trap, recorded so nobody re-chases it: fetching the `pub-*.r2.dev`
  URL with Python's default user agent returns **403, Cloudflare error 1010**. That
  is bot filtering, *not* public access being off — Chrome, curl and Go-http-client
  all get 200 for the same object.
- **The studio is open to every signed-in user, and it is a shared space with no
  separation inside it.** Decided 2026-07-31, replacing the admin-only gate it
  shipped with a few hours earlier. What that means concretely, so it is not a
  surprise later:

  - Any signed-in user sees **every** studio board and **every** image in it.
  - Any signed-in user can rename or **delete any board — which deletes its
    images** (cached media files survive; the catalogue entries do not).
  - Generation is **not budget-capped and writes no `UsageRecord`**, so one person
    can spend the shared Seedream key with nothing to stop them and nothing in the
    ledger to show it.

  This is the accepted cost of leaving the studio unscoped. Scoping it to a project
  fixes all three at once — §5 Phase 1 and Phase 4.

  Mechanically: the six `/api/flowstudio/*` routes call
  `resource_guard.require_signed_in`, a new gate meaning "has an account, not the
  public". Two existing object guards were widened to match, each narrowed to the
  studio's own rows rather than opened generally:

  - `authorize_reference` — rows with a `source_board_id` (and no project) are the
    studio's, so any account may touch them. Rows with neither stay admin-only.
  - `authorize_request` — node-less requests **of type `flow_gen_image`** may be
    polled by any account; every other node-less request stays admin-only.

  `require_signed_in` is registered in `test_route_authorization.py`'s marker list,
  so those routes are still counted as gated rather than exempted.

- **Anonymous callers pass locally**, exactly as `/api/references` already does,
  because `FLOWBOARD_REQUIRE_AUTH` is unset in dev. Setting
  `FLOWBOARD_REQUIRE_AUTH=1` closes it — `require_signed_in` returns 401 for an
  anonymous caller once it is on.
