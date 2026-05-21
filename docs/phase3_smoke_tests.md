# Phase 3 — Frontend smoke tests

Manual checks to run after a fresh build of the Phase 3 hierarchy views.
Frontend has no test framework yet, so these stand in for an automated
smoke suite. Run them locally; failures block the Phase 3 stop-point.

## Prerequisites

- Backend running on `localhost:8101` (Postgres + Alembic head)
- Frontend served from `localhost:5173` via `npm run dev`
- Chrome extension installed (only needed for actual generation tests)

## Setup

```bash
# Backend
cd agent && uv run alembic upgrade head
cd agent && uv run uvicorn flowboard.main:app --reload --port 8101

# Frontend
cd frontend && npm run dev
```

Hit `http://localhost:5173` — should land on `/projects` automatically.

## Test matrix

### T1 — Project create

1. Click **New project** in the sidebar.
2. Type `Anime Test` → Create.
3. Expect: route changes to `/projects/<uuid>`, ProjectDashboard renders.
4. Sidebar shows `Anime Test` highlighted.

### T2 — Project Bible round-trip

1. On the ProjectDashboard, edit Art style: `cel-shaded 90s OVA`.
2. Color palette: `teal, amber, ink black`.
3. Click **Save bible**. Wait for `Saved` state.
4. Refresh the browser (F5).
5. Expect: bible fields rehydrate to the saved values.

### T3 — Scene + shot create

1. On the ProjectDashboard, **+ Add scene** → name `Rainy rooftop`.
2. Route changes to `/scenes/<uuid>`.
3. Add **Scene Bible** text, save.
4. Click **+ Add shot**.
5. Route changes to `/shots/<uuid>` (ShotEditor with empty canvas).
6. Expect breadcrumb: `Projects / Anime Test / Rainy rooftop / Shot #1`.

### T4 — Canvas parity with pre-Phase-3 Board

1. From inside ShotEditor, open AddNodePalette → drop an **Image** node.
2. Drag from image node's output handle to empty canvas → DropAddPopover
   appears → pick **Video**.
3. Expect: a Video node spawns, edge wires from Image → Video.
4. Backspace-select Video node → it's deleted along with its edge.

### T5 — URL deep link

1. Copy the ShotEditor URL.
2. Open it in a new tab.
3. Expect: ShotEditor loads with the canvas, breadcrumb, and node graph
   restored. Project + scene context resolved via the API responses.

### T6 — Project switching mid-generation (state-leak check)

This is the canonical "no leak" test from Phase 3 stop-point checklist.

1. Create **Project A** with **Scene A1** and **Shot A1**.
2. Inside `Shot A1`, add an Image node and dispatch a generation
   (Flow / Dreamina — any provider that takes more than ~3 seconds).
3. **While the generation is still running**, navigate via URL bar to
   `/projects` and create **Project B** → **Scene B1** → **Shot B1**.
4. Inside `Shot B1`, expect the canvas to be **empty** — no nodes from
   Shot A1 should leak across.
5. Open the Activity bell → expect the in-flight job from Shot A1 still
   listed as `running`.
6. Navigate back to `Shot A1` (via sidebar → Project A → Scene A1 → list).
7. Expect: workflow nodes restored, the running job eventually completes
   and updates the original Image node's `mediaId` (NOT a node in Shot B1).
8. No `console.error` logged during the round-trip.

### T7 — localStorage migration

1. Open DevTools → Application → Local Storage.
2. Manually set `flowboard.activeBoardId = 42` (the legacy key).
3. Refresh.
4. Expect: `flowboard.activeBoardId` is **gone** after page load. App
   lands on `/projects` and is fully usable (no white screen, no
   `parseInt(NaN)` snap-back to the first project).

### T8 — Asset library filters (Phase 3 client-side)

1. Save 2-3 references from the Storyboard / Image nodes (★ button).
2. Open `/projects/<id>/library`.
3. Search by label substring → list shrinks.
4. Toggle "Pinned only" → only pinned refs show.
5. Note the **Scope: all projects** pill — Phase 3 doesn't filter by
   project on the server; Phase 4 will.

### T9 — Cost dashboard

1. Navigate to `/projects/<id>/cost`.
2. Expect: a `$0.00 USD` total card (no completed jobs yet) plus the
   Phase 7 hint. Hit `/api/projects/<id>/cost` via curl to verify the
   backend route works:
   ```bash
   curl -s localhost:8101/api/projects/<id>/cost
   # {"cost_usd": 0.0}
   ```

### T10 — Script → shots stub (Phase 6 stub)

1. Inside SceneView, click **Script → shots**.
2. Paste 3 paragraphs separated by blank lines.
3. Expect: dialog shows `Will create 3 shot(s)`.
4. Click **Create 3 shots** → 3 shots appear in the shots list, each
   with the corresponding paragraph as `script_text`.
5. The LLM parser (camera angle / characters / environment) is **NOT**
   wired today — that lands in Phase 6.

### T11 — Cascade delete

1. Inside ProjectDashboard, click ✕ on a scene → confirm.
2. Expect: scene + its shots vanish from the lists.
3. Backend: `select count(*) from shot where scene_id = '<uuid>'` returns 0.

## Stop-point checklist

- [ ] T1 passes (project create)
- [ ] T2 passes (bible round-trip)
- [ ] T3 passes (scene + shot create)
- [ ] T4 passes (canvas parity)
- [ ] T5 passes (deep link)
- [ ] T6 passes (project switching mid-generation does not leak state)
- [ ] T7 passes (localStorage migration)
- [ ] `npm run build` exits 0
- [ ] `uv run pytest -q` (in `agent/`) reports 463 passed
