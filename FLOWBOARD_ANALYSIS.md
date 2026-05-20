# Flowboard — Reconnaissance Analysis

Adaptation target: fork Flowboard from an e-commerce product-video tool into a multi-project anime narrative video generation tool. This document is **reconnaissance only** — no code changes are proposed here. All claims are grounded in files actually read; speculation is flagged.

## Repo layout vs. README

The repo on `main` (commit `711c62f`) matches the README's high-level claim: `agent/` (Python FastAPI), `frontend/` (React + Vite + React Flow + Zustand), `extension/` (Chrome MV3), `docs/`, `storage/`. Surprises vs. the README:

- README still mentions `claude` (CLI) as the LLM path, but `agent/flowboard/services/llm/` is a full **multi-provider abstraction** (Claude / Gemini / OpenAI) routed through a registry — the CLI module is now one provider among three. Important for adaptation: the multi-LLM extension layer the user asks for is already half-built.
- A `Reference` model + `/api/references` exists for a "cross-board library" feature not documented in the brief — relevant for the target's asset-library requirement.
- `Storyboard` is an actual node type and a worker handler (`gen_storyboard`), with parent/child shot trees and **per-level BFS dispatch + progressive persistence** — far closer to "scene → shot hierarchy" than the brief suggests.
- `docs/design/stitch-phase1/` contains 10 HTML mocks (Figma exports) of the UI surfaces — useful when reasoning about UX changes.
- `.omc/plans/` is a planning-doc directory but is gitignored content-wise — only mention I see in code is the path string in comments.

---

## A. Tech Stack (actual versions from lockfiles)

### Backend — `agent/requirements.txt`
Unpinned minimums (no `==`):

| Package | Lower bound |
|---|---|
| fastapi | `>=0.115` |
| uvicorn[standard] | `>=0.30` |
| sqlmodel | `>=0.0.22` |
| pydantic | `>=2.8` |
| websockets | `>=12.0` |
| python-multipart | `>=0.0.9` |
| httpx | `>=0.27` |

Resolved in `agent/.venv/lib/python3.14/site-packages/`: sqlmodel-0.0.38, sqlalchemy-2.0.49, fastapi (current), starlette-1.0.0, uvicorn-0.47.0, websockets-16.0, pydantic-core-2.46.4, httpcore-1.0.9, h11-0.16.0, httptools-0.7.1, python-dotenv-1.2.2, certifi-2026.4.22. **No anthropic / openai / google-generativeai SDK is listed** — the LLM providers are all subprocess-based (Claude CLI, Gemini CLI, codex CLI) plus a future-Python-SDK OpenAI API branch (see `services/llm/openai.py:1-50` which appears to roll its own httpx calls). Python target: **3.11+** per README badge but the active venv is **3.14**.

Flags / oddities:
- No test runner pinned (pytest is implied by `agent/tests/`).
- No formal linter / type-checker pinned.
- No anthropic / openai / google-generativeai SDKs (target's "Claude API / OpenAI API as new providers" is greenfield — see [agent/flowboard/services/llm/](agent/flowboard/services/llm/)).

### Frontend — `frontend/package.json` (v1.2.12)

Dependencies:

| Package | Version |
|---|---|
| @xyflow/react | `^12.3.5` |
| react | `^18.3.1` |
| react-dom | `^18.3.1` |
| zustand | `^5.0.0` |

devDependencies:

| Package | Version |
|---|---|
| @types/react | `^18.3.11` |
| @types/react-dom | `^18.3.1` |
| @vitejs/plugin-react | `^4.3.2` |
| typescript | `^5.6.2` |
| vite | `^5.4.9` |

Notes:
- **No testing framework on the frontend** (no vitest / jest / playwright / cypress). All claims about correctness are TypeScript + manual.
- **No ESLint / Prettier**. `"lint": "tsc -b --noEmit"` is the only lint command.
- **No router** (single-board SPA — see Section I).
- No CSS framework / Tailwind / shadcn — hand-written CSS in [styles.css](frontend/src/styles.css).
- No data-fetching layer (TanStack Query) — bare `fetch` wrapped in `api/client.ts`.

### Extension — `extension/manifest.json`

| Field | Value |
|---|---|
| manifest_version | 3 |
| name | "Flowboard Bridge" |
| version | "0.0.5" |
| permissions | storage, alarms, tabs, webRequest, scripting, declarativeNetRequest |
| host_permissions | `aisandbox-pa.googleapis.com`, `labs.google`, `127.0.0.1:8101`, `localhost:8101` |
| content_scripts | `https://labs.google/fx/tools/flow*` |

No dependencies (single-file vanilla JS service worker; ~700 LOC in [background.js](extension/background.js)).

### Deprecation / age flags
- Python **3.14 in the active venv** is bleeding-edge (released Oct 2025). README claims 3.11+ baseline.
- React **18** is one major behind React 19 (released Dec 2024). Not EOL, but a fork would inherit the upgrade debt.
- Vite **5** is one major behind Vite 6.
- Zustand **5** is current.
- SQLModel **0.0.x** has never reached 1.0 — known to be slow-moving.
- `manifest_version: 3` is current.
- No production dependency on a third-party LLM SDK — providers are wire-protocol clients in code.

---

## B. Database Schema

All tables live in [agent/flowboard/db/models.py](agent/flowboard/db/models.py). Backend: **SQLite** (no Postgres). Session config in [agent/flowboard/db/session.py:1-60](agent/flowboard/db/session.py) — single connection string `sqlite:///{DB_PATH}` with `PRAGMA foreign_keys=ON` enabled at connect time. `JSON` columns are SQLAlchemy generic JSON (not JSONB) — SQLite stores them as TEXT and the SQLModel layer JSON-encodes / decodes on read.

### Table: `board`
| Column | Type | Default | Null | FK |
|---|---|---|---|---|
| id | int PK | autoinc | no | — |
| name | str | — | no | — |
| created_at | datetime | `utcnow()` | no | — |

### Table: `node`
| Column | Type | Default | Null | FK / Index |
|---|---|---|---|---|
| id | int PK | autoinc | no | — |
| board_id | int | — | no | FK board.id, INDEX |
| short_id | str | — | no | INDEX (not unique) |
| type | str | — | no | — |
| x, y | float | 0.0 | no | — |
| w, h | float | 240/160 | no | — |
| data | JSON | `{}` | no | — |
| status | str | `"idle"` | no | — |
| created_at | datetime | `utcnow()` | no | — |

**Per-node JSON shape** (from `FlowboardNodeData` in [frontend/src/store/board.ts:43-106](frontend/src/store/board.ts) and the worker handlers): `type`, `shortId`, `title`, `prompt`, `mediaId`, `mediaIds` (array of `string|null` — null entries are content-filter-blocked variants), `slotErrors`, `variantCount`, `aspectRatio` (Flow's `IMAGE_ASPECT_RATIO_*` enum), `aiBrief`, `aiBriefStatus`, `autoPromptStatus`, `renderedAt`, `imageModel`, `videoQuality`, `charCountry`, `charVibe`, `charGender`, `error`, plus Storyboard-only `shots`, `shotCount`, `narrativeSeed`.

### Table: `edge`
| Column | Type | Default | Null | FK / Index |
|---|---|---|---|---|
| id | int PK | autoinc | no | — |
| board_id | int | — | no | FK board.id, INDEX |
| source_id | int | — | no | FK node.id |
| target_id | int | — | no | FK node.id |
| kind | str | `"ref"` | no | — |
| source_variant_idx | int | NULL | yes | — |

`source_variant_idx` was added by a runtime ALTER TABLE migration in [session.py:45-51](agent/flowboard/db/session.py); the column is `None` when the edge falls back to the source's active `mediaId`. Long comment in models.py explains why per-edge variant pinning is needed (Flow doesn't bind output[i] to input[i] in multi-variant batches).

### Table: `request` (worker queue rows)
| Column | Type | Default | Null | FK / Index |
|---|---|---|---|---|
| id | int PK | autoinc | no | — |
| node_id | int | NULL | yes | FK node.id, INDEX |
| type | str | — | no | — |
| params | JSON | `{}` | no | — |
| status | str | `"queued"` | no | — |
| result | JSON | `{}` | no | — |
| error | str | NULL | yes | — |
| created_at | datetime | `utcnow()` | no | — |
| finished_at | datetime | NULL | yes | — |

`type` values observed: `proxy`, `create_project`, `gen_image`, `gen_video`, `edit_image`, `gen_storyboard`, `retry_storyboard_shot`, plus LLM activity types `auto_prompt`, `auto_prompt_batch`, `auto_prompt_storyboard`, `vision`, `planner` (these last five are also written here by [services/activity.py](agent/flowboard/services/activity.py) as a unified activity log — see Section C `/api/activity`).

### Table: `asset` (auto-managed media cache index)
| Column | Type | Default | Null | FK / Index |
|---|---|---|---|---|
| id | int PK | autoinc | no | — |
| node_id | int | NULL | yes | FK node.id, INDEX |
| kind | str | — | no | image / video / thumbnail |
| uuid_media_id | str | NULL | yes | INDEX, **UNIQUE** |
| url | str | NULL | yes | Latest signed GCS URL (expires) |
| local_path | str | NULL | yes | Cached file in `storage/media/` |
| mime | str | NULL | yes | — |
| created_at | datetime | `utcnow()` | no | — |

### Table: `reference` (user-curated cross-board library)
| Column | Type | Default | Null | FK / Index |
|---|---|---|---|---|
| id | int PK | autoinc | no | — |
| media_id | str | — | no | INDEX, **UNIQUE** |
| url | str | NULL | yes | — |
| label | str | `""` | no | — |
| kind | str | — | no | `"image"`/`"character"`/`"visual_asset"`/`"storyboard_shot"` |
| ai_brief | str | NULL | yes | — |
| aspect_ratio | str | NULL | yes | — |
| tags | JSON list | `[]` | no | — |
| pinned | bool | False | no | — |
| position | int | 0 | no | — |
| source_board_id | int | NULL | yes | FK board.id, INDEX |
| source_node_short_id | str | NULL | yes | — |
| created_at | datetime | `utcnow()` | no | — |

### Table: `chatmessage`
| Column | Type | Default | Null | FK / Index |
|---|---|---|---|---|
| id | int PK | autoinc | no | — |
| board_id | int | — | no | FK board.id, INDEX |
| role | str | — | no | user/assistant/system |
| content | str | — | no | — |
| mentions | JSON list | `[]` | no | array of short_ids |
| created_at | datetime | `utcnow()` | no | — |

### Table: `plan`
| Column | Type | Default | Null | FK / Index |
|---|---|---|---|---|
| id | int PK | autoinc | no | — |
| board_id | int | — | no | FK board.id, INDEX |
| spec | JSON | `{}` | no | `{nodes:[{tmp_id,type,params}], edges:[{from,to,kind}], layout_hint}` |
| status | str | `"draft"` | no | draft/approved/running/done/failed |
| created_at | datetime | `utcnow()` | no | — |

### Table: `planrevision`
`id, plan_id (FK,INDEX), rev_no, spec JSON, edits JSON, created_at`. (Plumbed in schema; no route reads/writes it — see Section L.)

### Table: `pipelinerun`
| Column | Type | Default | Null | FK / Index |
|---|---|---|---|---|
| id | int PK | autoinc | no | — |
| plan_id | int | — | no | FK plan.id, INDEX |
| status | str | `"pending"` | no | pending/running/done/failed |
| started_at | datetime | NULL | yes | — |
| finished_at | datetime | NULL | yes | — |
| error | str | NULL | yes | — |

### Table: `boardflowproject`
| Column | Type | Default | Null | FK / Index |
|---|---|---|---|---|
| board_id | int PK | — | no | FK board.id |
| flow_project_id | str | — | no | — |
| created_at | datetime | `utcnow()` | no | — |

1:1 binding from local board → remote Google Flow `projectId`. Important for adaptation: this **single FK is the only "project" concept in the schema** — there is no abstract Project table; "Board" *is* the project.

### ER overview

```
board ──┬─< node ──< edge (source_id, target_id → node)
        │         └─< asset (cache index)
        │         └─< request (worker queue rows)
        ├──< chatmessage
        ├──< plan ──< planrevision
        │         └─< pipelinerun
        └──1 boardflowproject (1:1 link to remote Flow project)

reference (standalone, user library) ─ source_board_id (nullable FK board.id)
```

**JSON / JSONB note**: every JSON column is generic SQLAlchemy `JSON`, stored as TEXT in SQLite. No `JSON1`/`json_extract` calls anywhere — search shows only one `PRAGMA` (the FK enable) in [session.py:18](agent/flowboard/db/session.py). Migrating to Postgres should be straightforward at the column level (swap JSON→JSONB).

---

## C. REST API Surface

All routes mounted under `/api/*` by [main.py:86-101](agent/flowboard/main.py) except media bytes (`GET /media/...`) and the extension callback `/api/ext/callback`.

| Method | Path | Purpose | Request body | Response shape |
|---|---|---|---|---|
| GET  | `/api/health` | Liveness + ext stats | — | `{ok, extension_connected, ws_stats}` |
| POST | `/api/ext/callback` | Extension delivers API response (HMAC-authed via `X-Callback-Secret`) | `{id, status, data, error?}` | `{ok:bool}` |
| GET  | `/api/boards` | List boards | — | `[Board, …]` |
| POST | `/api/boards` | Create board | `{name}` | `Board` |
| GET  | `/api/boards/{id}` | Board detail with nodes+edges | — | `{board, nodes:[Node…], edges:[Edge…]}` |
| PATCH | `/api/boards/{id}` | Rename | `{name}` | `Board` |
| DELETE | `/api/boards/{id}` | Cascade-delete board + children | — | `{deleted:id}` |
| GET  | `/api/boards/{id}/project` | Read bound Flow project | — | `{flow_project_id, created:false}` (404 if unbound) |
| POST | `/api/boards/{id}/project` | Idempotent: bootstrap or fetch Flow project | — | `{flow_project_id, created:bool}` |
| GET  | `/api/boards/{id}/chat` | Chat history | — | `[ChatMessage…]` |
| POST | `/api/chat` | Send chat → planner → maybe Plan | `{board_id, message, mentions[]}` | `{user, assistant, plan?}` |
| POST | `/api/nodes` | Create node | `{board_id, type, x, y, w?, h?, data?, status?}` | `Node` |
| PATCH | `/api/nodes/{id}` | Partial update with **shallow-merge** of `data` (`null` = delete key) | `{x?, y?, w?, h?, data?, status?}` | `Node` |
| DELETE | `/api/nodes/{id}` | Delete + cascade edges | — | `{ok, deleted_edges}` |
| POST | `/api/edges` | Create edge | `{board_id, source_id, target_id, kind?, source_variant_idx?}` | `Edge` |
| PATCH | `/api/edges/{id}` | Update variant pin only | `{source_variant_idx?}` | `Edge` |
| DELETE | `/api/edges/{id}` | Delete edge | — | `{ok}` |
| GET  | `/api/references` | List with `?q=&pinned_first=&limit=` | — | `[Reference…]` |
| POST | `/api/references` | Idempotent save by `media_id` | `{media_id, kind, label?, ai_brief?, aspect_ratio?, url?, source_board_id?, source_node_short_id?, tags?}` | `Reference` |
| PATCH | `/api/references/{id}` | Rename/pin/reorder/tags | `{label?, pinned?, position?, tags?}` | `Reference` |
| DELETE | `/api/references/{id}` | 204; cache file untouched | — | — |
| POST | `/api/requests` | Enqueue worker request | `{node_id?, type, params}` | `Request` |
| GET  | `/api/requests/{id}` | Poll status | — | `Request` |
| POST | `/api/requests/{id}/cancel` | Cancel only `queued` rows | — | `Request` (409 otherwise) |
| GET  | `/media/{media_id:path}` | Stream cached bytes; one-shot fetch on cache miss | — | `FileResponse` |
| GET  | `/api/media/{media_id}/status` | Cache hit/miss + reason | — | `{available, has_url, mime?, reason?}` |
| GET  | `/api/media/_debug/assets` | **Dev-only** Asset dump | — | `{count, rows[]}` |
| POST | `/api/upload` | Multipart image → Flow → cache + Asset row | multipart `project_id, node_id?, file` | `{media_id, mime, size, width?, height?, aspect_ratio?}` |
| POST | `/api/upload-url` | Server-side fetch a public URL → same pipeline | `{url, project_id, node_id?}` | same |
| GET  | `/api/plans/{plan_id}` | Read plan row | — | `Plan` |
| POST | `/api/plans/{plan_id}/run` | Materialise + spawn `run_pipeline` task | — | `PipelineRun` |
| GET  | `/api/pipeline-runs/{run_id}` | Poll status | — | `PipelineRun` |
| POST | `/api/vision/describe` | Vision LLM → text aiBrief | `{media_id}` | `{media_id, description}` |
| POST | `/api/prompt/auto` | Single auto-prompt via upstream walk | `{node_id, camera?}` | `{node_id, prompt}` |
| POST | `/api/prompt/auto-batch` | N pose-distinct prompts (1..8) | `{node_id, count, camera?}` | `{node_id, prompts[]}` |
| GET  | `/api/auth/me` | Cached Google profile + paygate tier | — | `{email, name, picture, verified_email, paygate_tier, sku, credits}` |
| POST | `/api/auth/logout` | Notify ext + drop in-mem identity | — | `{ok, extension_notified}` |
| POST | `/api/auth/scan` | Diagnostic + nudge userinfo / fetch tier | — | `{extension_connected, has_user_info, has_paygate_tier, userinfo_nudged, tier_fetched}` |
| GET  | `/api/llm/providers` | Provider list with availability | — | `[{name, supportsVision, available, configured, requiresKey, mode}]` |
| PUT  | `/api/llm/providers/{name}` | Set/clear API key (OpenAI only) | `{apiKey?}` | `{ok}` |
| POST | `/api/llm/providers/{name}/test` | Ping + latency | — | `{ok, latencyMs?, error?}` |
| GET  | `/api/llm/config` | Feature → provider mapping | — | `{auto_prompt, vision, planner, configured}` |
| PUT  | `/api/llm/config` | Update mapping | `{auto_prompt?, vision?, planner?}` | `{ok}` |
| POST | `/api/llm/debug/reset-probe` | Force re-probe Claude CLI | — | `{ok, claude_available}` |
| GET  | `/api/activity` | Cursor-paginated activity feed | query: `limit, before_id, type` | `{items[], next_before_id}` |
| GET  | `/api/activity/{id}` | Full row incl. params/result/error | — | detailed row |

**Routes that look unused or experimental**: `/api/media/_debug/assets` (explicit dev-only comment), `/api/llm/debug/reset-probe` (debug). No frontend code calls `/api/auth/logout` from `api/client.ts`. `PlanRevision` is in the schema but no route reads/writes it.

---

## D. Node Type Inventory

### Backend enum
[agent/flowboard/routes/nodes.py:13](agent/flowboard/routes/nodes.py):
```python
NodeType = Literal["character", "image", "video", "prompt", "note", "visual_asset"]
```

⚠️ **Inconsistency**: backend allows 6 types here, but the worker + frontend both also use `"Storyboard"` (capitalised) — created via [POST /api/nodes](agent/flowboard/routes/nodes.py) it would fail Pydantic validation. **Either it bypasses validation through `routes/nodes.py` somehow, or the frontend uses a non-public create path. Needs user confirmation — see Section L.**

Frontend canonical list — [frontend/src/api/client.ts:113](frontend/src/api/client.ts):
```ts
export type NodeType = "character" | "image" | "video" | "prompt" | "note" | "visual_asset" | "Storyboard";
```

Frontend palette ([AddNodePalette.tsx:11-19](frontend/src/canvas/AddNodePalette.tsx)) offers all seven.

### Per-type details

The canvas uses a single `NodeCard` React component ([NodeCard.tsx](frontend/src/canvas/NodeCard.tsx) — 1651 LOC) that internally branches by `data.type`. Inputs/outputs below are inferred from `collectUpstreamRefMediaIds` ([store/generation.ts:88-119](frontend/src/store/generation.ts)) and the worker handlers.

| kind | Inputs (upstream types it accepts as ref) | Outputs (data fields downstream consume) | Settings/params | Worker task | External provider |
|---|---|---|---|---|---|
| `character` | drag-drop upload OR generated from upstream `image` ref | `mediaId`, `mediaIds[]`, `aiBrief`, `aspectRatio`, `charCountry`, `charVibe`, `charGender` | upload-only OR `prompt`+gen_image with character refs | none directly (uploads sync via `/api/upload`) or `gen_image` | Flow `uploadImage` or `batchGenerateImages` |
| `image` | `character`, `image`, `visual_asset`, `Storyboard` | `mediaId`, `mediaIds[]`, `aiBrief`, `aspectRatio`, `imageModel` | `prompt`, `variant_count` (1-4), `aspect_ratio`, `image_model` (`NANO_BANANA_PRO`/`NANO_BANANA_2`), per-variant `prompts[]` | `gen_image` (new) / `edit_image` (refine) | Flow `batchGenerateImages` |
| `video` | exactly one upstream image-bearing node (single or multi-variant via `start_media_ids`) | `mediaId`, `mediaIds[]`, `slotErrors[]`, `videoQuality` | `prompt`, `aspect_ratio` (VIDEO_*), `video_quality` (`fast`/`lite`/`quality`/`lite_relaxed`/`fast_relaxed`), `start_media_id(s)` | `gen_video` (polls via `check_async`) | Flow `batchAsyncGenerateVideoStartImage` (Veo 3.1 i2v) |
| `prompt` | reads as upstream "direction / style notes" in [prompt_synth._format_user_message](agent/flowboard/services/prompt_synth.py) | `prompt` (text-only; no media) | free-text only | none | none |
| `note` | **NOT** a ref source; explicitly excluded from upstream walk | text only | free-text only | none | none |
| `visual_asset` | drag-drop upload of product/wardrobe imagery | `mediaId`, `aiBrief`, `aspectRatio` | upload-only or pasted from Reference library | none (sync via `/api/upload`) | Flow `uploadImage` |
| `Storyboard` | optional upstream chars/images for refs; planner produces tree | `shots[]` (each shot has its own `mediaId`+status), `shotCount`, `narrativeSeed` | `shot_count` (1-8), `narrative_seed`, `aspect_ratio`, `image_model` | `gen_storyboard`, `retry_storyboard_shot` | Flow `batchGenerateImages` (roots, ≤4/chunk) + `flow_media_edit` (children) |

**REF_SOURCE_TYPES** — only `character`, `image`, `visual_asset`, `Storyboard` produce IMAGE_INPUT_TYPE_REFERENCE inputs to Flow ([generation.ts:88](frontend/src/store/generation.ts) and [prompt_synth.py:232](agent/flowboard/services/prompt_synth.py)). `prompt` and `note` are context-only.

Handle semantics in React Flow: there is no separate handle name registry. Each NodeCard renders the standard ReactFlow `Handle` source/target on left/right ends; the **single** connector means "this output feeds that input as a reference" — there is no typed handle (e.g. "still-out" vs "video-out"). All wiring is uniform; semantics come from the source node's `data.type`.

---

## E. Worker Queue & Task Catalog

### Queue mechanism
**Custom in-process** — see [worker/processor.py:799-922](agent/flowboard/worker/processor.py). It is **NOT** Celery, RQ, or Arq. The implementation:

- One `asyncio.Queue[int]` of request ids inside a single `WorkerController` instance.
- Single consumer loop in `start()` (sequential — no concurrent dispatch across types) that pops one rid, sets `status="running"`, awaits the handler, then writes back result + status.
- Lifecycle wired in [main.py:52-73](agent/flowboard/main.py) — `worker_task = asyncio.create_task(worker.start(), name="request-worker")` in the FastAPI lifespan.
- On agent startup, `_recover_orphan_running_requests()` flips any pre-existing `running` rows to `failed` with error `agent_restart_lost` — see [main.py:32-49](agent/flowboard/main.py). **In-flight jobs are lost across restarts.**
- No persistent queue — `enqueue()` only puts the rid on the in-memory `asyncio.Queue`. If the agent restarts before the worker pops, the DB row stays `queued` forever (no recovery scan for queued rows).
- Cancellation: only `queued` rows can be cancelled via `POST /api/requests/{id}/cancel` — running jobs cannot be interrupted. The worker re-reads the row status before processing (drift guard, [processor.py:856-860](agent/flowboard/worker/processor.py)).
- **No retry / backoff policy.** A handler that returns an `error` string just stamps the row as `failed`; the next user click is the only retry mechanism.

### Task catalog

All handlers live in `worker/processor.py`. Common contract: `async (params: dict) → tuple[dict, Optional[str]]` — `(result_payload, error_string_or_none)`.

| Task | Entry | Input contract | Calls externally | Result / DB updates | Sync vs async |
|---|---|---|---|---|---|
| `proxy` | `_handle_proxy` | `{url, method?, headers?, body?}` (url must start with `https://aisandbox-pa.googleapis.com/`) | `flow_client.api_request` (extension proxy) | raw response | sync RPC (single round-trip) |
| `create_project` | `_handle_create_project` | `{name|title?, tool="PINHOLE"}` | TRPC `project.createProject` | `{project_id, raw}` | sync |
| `gen_image` | `_handle_gen_image` | `{prompt, project_id, aspect_ratio?, paygate_tier?, ref_media_ids?, variant_count?, prompts?[], image_model?}` | `FlowSDK.gen_image` → `batchGenerateImages` (with IMAGE_GENERATION captcha) | `{media_ids[], media_entries[], raw}` + `media_service.ingest_urls` side-effect | sync (Flow image gen returns inline) |
| `edit_image` | `_handle_edit_image` | `{prompt, project_id, source_media_id, ref_media_ids?, aspect_ratio?, paygate_tier?, image_model?}` | `FlowSDK.edit_image` (same URL, with BASE_IMAGE first) | same | sync |
| `gen_video` | `_handle_gen_video` | `{prompt, project_id, start_media_id\|start_media_ids[], aspect_ratio?, paygate_tier?, video_quality?}` | `FlowSDK.gen_video` (VIDEO_GENERATION captcha) → poll loop via `check_async` | `{media_ids[] (positional, null entries for blocked slots), media_entries[], slot_errors[], op_errors{}, partial_error?}` | **polling**: `VIDEO_POLL_INTERVAL_S=10s × VIDEO_POLL_MAX_CYCLES=42` = 7 min ceiling |
| `gen_storyboard` | `_handle_gen_storyboard` | `{shot_count(1-8), project_id, narrative_seed?, global_ref_media_ids?, image_model?, aspect_ratio?, paygate_tier?, shot_prompts?, shot_parents?}` | `auto_prompt_storyboard` (LLM plan) → Phase A `gen_image` of roots in ≤4/chunk → BFS Phase B `edit_image` per level. Progress persisted to `Node.data.shots` after each phase. | `{shots[{idx, prompt, parentShotIdx, mediaId, status, error}], media_ids[], node_status}` | sync (no operation polling; image gen is inline) |
| `retry_storyboard_shot` | `_handle_retry_storyboard_shot` | `{shot_idx, …}` (reads existing `Node.data.shots[shot_idx]`) | `gen_image` (root) or `edit_image` (child, using parent.mediaId) | `{shot_idx, media_id, media_ids[]}` | sync |

LLM activity types (`auto_prompt`, `auto_prompt_batch`, `auto_prompt_storyboard`, `vision`, `planner`) are **NOT** dispatched through this queue — they are written as Request rows directly by `services/activity.record_activity` only for the activity-log surface. The actual LLM call runs inline inside the HTTP handler (e.g. POST `/api/prompt/auto`).

---

## F. Google Flow Integration (Extension Bridge)

This is the most idiosyncratic part of the codebase. Three actors:
1. **Agent** — `:8101` HTTP + **`:9223` WS**.
2. **Extension service worker** ([background.js](extension/background.js)) — Bearer token sniffer + WS client + fetch proxy.
3. **Injected MAIN-world script** ([injected.js](extension/injected.js)) on `labs.google/fx/tools/flow*` — only purpose is reCAPTCHA solving with the page's own `grecaptcha` object.

### Endpoints intercepted

The extension does NOT intercept Flow API responses (older flowkit pattern). Instead the agent **drives** the Flow API and the extension only proxies authenticated `fetch` calls. The agent issues calls to:

- `https://aisandbox-pa.googleapis.com/v1/projects/{id}/flowMedia:batchGenerateImages` (image gen + edit)
- `https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartImage` (Veo i2v)
- `https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus` (poll)
- `https://aisandbox-pa.googleapis.com/v1/flow/uploadImage` (upload)
- `https://aisandbox-pa.googleapis.com/v1/media/{id}?clientContext.tool=PINHOLE` (Low-Priority workflow poll — inline base64 MP4)
- `https://aisandbox-pa.googleapis.com/v1/credits` (paygate tier resolution — fetched server-side from the agent with the captured Bearer)
- `https://labs.google/fx/api/trpc/project.createProject` (TRPC; no captcha)
- `https://www.googleapis.com/oauth2/v2/userinfo` (extension fetches this directly, not via proxy)

The extension's webRequest listener watches `https://aisandbox-pa.googleapis.com/*` and `https://labs.google/*` **only for Authorization headers**, not response bodies. See [background.js:82-121](extension/background.js).

### How the auth token is captured

Extension's `chrome.webRequest.onBeforeSendHeaders` fires on every outbound request from the user's Flow tab. When it sees a header starting with `Bearer ya29.` (Google's OAuth2 v2 access-token prefix), it extracts the token and emits a `token_captured` WS message to the agent **only when the token rotates** (defensive dedupe — older versions emitted on every request which trashed `/v1/credits`).

```js
// background.js:82-121 (excerpt)
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (!details?.requestHeaders?.length) return;
    const authHeader = details.requestHeaders.find(
      (h) => h.name?.toLowerCase() === 'authorization',
    );
    const value = authHeader?.value || '';
    if (!value.startsWith('Bearer ya29.')) return;
    const token = value.replace(/^Bearer\s+/i, '').trim();
    if (!token) return;
    const tokenChanged = flowKey !== token;
    flowKey = token;
    metrics.tokenCapturedAt = Date.now();
    chrome.storage.local.set({ flowKey, metrics });
    if (tokenChanged) {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
      }
      fetchAndPushUserInfo(token);
    }
  },
  { urls: ['https://aisandbox-pa.googleapis.com/*', 'https://labs.google/*'] },
  ['requestHeaders', 'extraHeaders'],
);
```

### Message protocol

WebSocket envelope (agent ↔ extension):

```jsonc
// agent → extension (request)
{ "id": "<uuid>", "method": "api_request"|"trpc_request"|"get_status", "params": {…} }

// extension → agent (response — sent via HTTP callback, not WS, for resilience)
{ "id": "<uuid>", "status": <httpStatus>, "data": <responseJson>, "error"?: "<string>" }

// extension → agent (push)
{ "type": "extension_ready", "flowKeyPresent": bool, "tokenAge": ms }
{ "type": "token_captured",  "flowKey": "<token>" }
{ "type": "user_info",       "userInfo": {email, name, picture, verified_email, …} }
{ "type": "ping" } / { "type": "pong" }

// agent → extension (push)
{ "type": "callback_secret",       "secret": "<32-byte url-safe>" }   // sent on connect
{ "type": "logout" }
{ "type": "please_resend_userinfo" }
```

### WebSocket endpoint

`WS_HOST = "127.0.0.1"`, `EXTENSION_WS_PORT = 9223` per [config.py:8-10](agent/flowboard/config.py). README says 9222 — code says **9223** (README text and `main.py:61` log both say 9223; the README badge / one paragraph saying 9222 appears to be stale).

[main.py:22-26](agent/flowboard/main.py) hard-refuses to boot if `FLOWBOARD_WS_HOST` is non-loopback — the WS is **unauthenticated by design** and must not be network-reachable.

```python
if WS_HOST not in ("127.0.0.1", "localhost", "::1"):
    raise RuntimeError(
        f"FLOWBOARD_WS_HOST must be loopback (got {WS_HOST!r}); the extension WS "
        "is unauthenticated by design and must not be network-reachable."
    )
```

### Allowlist of Flow API methods

The extension's `handleApiRequest` (background.js:302) hard-checks `url.startsWith('https://aisandbox-pa.googleapis.com/')` and the agent's `_handle_proxy` ([processor.py:30-42](agent/flowboard/worker/processor.py)) duplicates this allowlist. TRPC mirror: `url.startsWith('https://labs.google/fx/api/trpc/')` in [background.js:593](extension/background.js).

Concrete Flow API methods invoked end-to-end:
- `flowMedia:batchGenerateImages` (gen_image + edit_image)
- `video:batchAsyncGenerateVideoStartImage` (i2v dispatch)
- `video:batchCheckAsyncVideoGenerationStatus` (i2v poll, old schema)
- `media/{id}` GET (Low-Priority workflow poll, new schema)
- `flow/uploadImage` (user image upload)
- `credits` (paygate tier fetched server-side by the agent itself with the captured Bearer — bypasses the extension)
- TRPC: `project.createProject` only

### How signed CDN URLs are persisted

Google Flow returns signed `flow-content.google` URLs in the gen response (`data.media[].image.generatedImage.fifeUrl`). The worker calls `media_service.ingest_urls()` on success — see [processor.py:134-141](agent/flowboard/worker/processor.py), [services/media.py:79-110](agent/flowboard/services/media.py). Each entry is upserted into the `asset` table keyed by `uuid_media_id` (UNIQUE). `local_path` stays NULL until a `GET /media/{id}` triggers `fetch_and_cache()` (lazy) which downloads the bytes once and writes them to `storage/media/{uuid}.{ext}`. After that the asset row gets `local_path` populated and signed-URL expiry doesn't matter.

For Low-Priority (workflow) videos, Flow returns base64-encoded MP4 inline on `/v1/media/{id}` — `ingest_inline_bytes` plants those bytes in cache without ever touching a URL ([media.py:113-145](agent/flowboard/services/media.py)).

URL fetch is gated to the prefix `https://flow-content.google/` ([media.py:36-42](agent/flowboard/services/media.py)) — defense-in-depth.

### HMAC `X-Callback-Secret`

- **Generation**: 32-byte URL-safe random token in `FlowClient.__init__` ([flow_client.py:53](agent/flowboard/services/flow_client.py)): `self._callback_secret = secrets.token_urlsafe(32)`. Regenerated on every agent process start — **not persisted**.
- **Distribution**: agent pushes it to the extension as the first WS message after connect ([ws_server.py:25-28](agent/flowboard/services/ws_server.py)):
  ```python
  await websocket.send(
      json.dumps({"type": "callback_secret", "secret": flow_client.callback_secret})
  )
  ```
- The extension stores it in `chrome.storage.local.callbackSecret` and attaches `X-Callback-Secret` to every `/api/ext/callback` POST ([background.js:281-287](extension/background.js)).
- **Verification**: HTTP handler `/api/ext/callback` uses `hmac.compare_digest` (constant-time) in [main.py:113-122](agent/flowboard/main.py):
  ```python
  if not x_callback_secret or not hmac.compare_digest(
      x_callback_secret, flow_client.callback_secret
  ):
      raise HTTPException(status_code=401, detail="invalid callback secret")
  ```
- No rotation policy. If the agent process restarts, the new secret arrives on the next WS reconnect; in-flight callbacks with the old secret are rejected (and the corresponding futures will time out at `DEFAULT_TIMEOUT=180s`).

### Why HTTP-callback instead of WS for responses
[background.js:279-291](extension/background.js): "Responses (`msg.id` present) go via HTTP callback — immune to WS drops. Falls back to WS on HTTP failure." A long-running operation (video poll, captcha solve) can outlive a stale WS connection; HTTP gives the response a durable delivery channel.

---

## G. Claude CLI Integration

### Subprocess invocation

[claude_cli.py:114-153](agent/flowboard/services/claude_cli.py):

```python
args: list[str] = [claude_bin, "-p", "--output-format", "json"]
if system_prompt:
    args += ["--append-system-prompt", system_prompt]
if attachments:
    seen_dirs: set[str] = set()
    for path in attachments:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and parent not in seen_dirs:
            seen_dirs.add(parent)
            args += ["--add-dir", parent]
    args += ["--permission-mode", "bypassPermissions"]
# prompt + `@<absolute_path>` tokens piped via stdin (avoids Windows .cmd argv issues)
result = subprocess.run(args, input=full_prompt.encode("utf-8"),
                        capture_output=True, timeout=timeout, text=False)
```

Key knobs:
- Output format JSON envelope: `{"type":"result","result":"<llm text>", "is_error"?, "subtype"?}`.
- Prompt + attachment tokens **piped via stdin** (not as `-p <prompt>`) to dodge Windows `.cmd` shim argv reparsing — a real bug that surfaced as "claude received NO real prompt and replied conversationally". Comment in [claude_cli.py:122-138](agent/flowboard/services/claude_cli.py) is the diary.
- Vision: attachments are listed as `@<abs_path>` tokens in the prompt; the CLI reads + base64-encodes them.
- For attachments to work: parent dir must be `--add-dir` allowlisted AND Read tool auto-approved via `--permission-mode bypassPermissions`.
- Probe (`--version`) cached in module global `_available` ([claude_cli.py:35](agent/flowboard/services/claude_cli.py)). `is_available(force=True)` re-probes.

### Argument structure per call site

- **Vision describe** ([services/vision.py:92-98](agent/flowboard/services/vision.py)): `run_llm("vision", "Describe this image.", system_prompt=_VISION_SYSTEM, attachments=[abs_path], timeout=120)`.
- **Auto-prompt (single)** ([prompt_synth.py:759-764](agent/flowboard/services/prompt_synth.py)): `run_llm("auto_prompt", user_msg, system_prompt=_image_or_video_system, timeout=90)`. **No attachments** — upstream context comes as text only (the LLM's job is composition, not vision).
- **Auto-prompt (batch)** ([prompt_synth.py:690-692](agent/flowboard/services/prompt_synth.py)): same call with batch suffix; expects JSON array; timeout=120.
- **Storyboard** ([prompt_synth.py:581-587](agent/flowboard/services/prompt_synth.py)): expects JSON object `{prompts[N], parents[N]}`; timeout=120.
- **Planner** ([services/planner.py:277-281](agent/flowboard/services/planner.py)): text in, fenced JSON block extracted via regex; no attachments.

Response parsing:
- Vision / auto-prompt single: plain string, stripped + length-capped.
- Auto-prompt batch: JSON array; strip ` ```json ` fences if present, then `json.loads`.
- Storyboard: JSON object with schema validation (length, parent index bounds, root must be `null`).
- Planner: regex `\`\`\`json\\s*(\\{.*?\\})\\s*\`\`\`` in [planner.py:141](agent/flowboard/services/planner.py); fallback "entire body is JSON" check; minimum-shape check via `_is_valid_plan_shape`.

Failure modes handled in `claude_cli.run_claude`:
- `FileNotFoundError` → `ClaudeCliError("claude CLI not found on PATH")`.
- `subprocess.TimeoutExpired` → `ClaudeCliError("claude CLI timed out after Xs")`.
- non-zero exit → `ClaudeCliError(f"claude CLI exited {n}: {stderr[:400]}")`.
- non-JSON stdout → `ClaudeCliError("claude CLI returned non-JSON output: ...")` with first 200 chars.
- envelope `is_error: true` → `ClaudeCliError(envelope['result'] or envelope['subtype'])`.
- `result` field not str → `ClaudeCliError("envelope missing string 'result' field")`.

`ClaudeProvider` ([services/llm/claude.py](agent/flowboard/services/llm/claude.py)) wraps all these into `LLMError` so callers can `except LLMError`.

### Verbatim system prompts (this is the high-value IP)

#### Vision — [services/vision.py:35-44](agent/flowboard/services/vision.py)
```
You are a visual asset annotator for a fashion / e-commerce media pipeline. Output one short factual sentence (max 200 characters) that describes the image. Focus on attributes useful for image generation: for a product → colour, material, design, fit, style; for a person → gender, apparent ethnicity, age range, expression, hair, outfit. No marketing language, no opinions, no preamble — just the description.
```

#### Auto-prompt image (single subject) — [prompt_synth.py:29-75](agent/flowboard/services/prompt_synth.py)
```
You are an image-generation prompt builder for a fashion / e-commerce media pipeline. Output ONE concise sentence (max 280 chars) for a photoreal shot combining the input briefs.

POSE — every shot must look like a real editorial / lookbook photo:
  • GAZE: the model's eyes MUST ENGAGE THE CAMERA — direct eye contact with the lens. No looking-away, no eyes-closed, no over-the-shoulder backshots, no profile-only poses. The face is always turned to camera.
  • EXPRESSION — CRITICAL: NEUTRAL CLOSED-MOUTH expression at all times. NO smiling, NO teeth visible, NO laughing, NO open mouth. A very soft, almost-imperceptible curl of the lips is the maximum. This is non-negotiable — open-mouth smiles get warped by Veo i2v downstream and cause face-identity drift across the clip. Use phrases like 'composed neutral expression', 'closed-mouth confident look', 'lips together'.
  • STANCE — pick ONE from this pool (rotate so generations stay diverse, do not repeat the same stance):
    · both hands in pockets, weight on one leg, slight hip pop
    · one hand brushing the collar / sleeve / hem of the garment
    · hand-on-hip, body angled three-quarters to camera
    · arms casually crossed at the chest, head tilted slightly
    · hand running through hair, head turned slightly to the side
    · one hand resting at the side of the face, playful or pensive
    · walking towards camera mid-stride, casual confidence
    · leaning weight on one hip with thumbs hooked into pockets
  • BODY ANGLE: pick straight-on, three-quarter, or slight side — as long as the face stays toward camera.
  • ATTITUDE: confident, charismatic, distinctive personality and presence (model 'aura'). Never stiff or generic.

When a product / wardrobe asset is in the inputs AND no location reference is present, the chosen pose must make the GARMENT the visual hero — knees-up or full upper-body framing. When a location reference IS present, balance the framing: the garment stays readable but the environment must be visible in frame (wider shot, knees-up to full-body so the setting reads).

Style: photoreal editorial fashion photography, sharp focus, soft even key light. BACKGROUND PRIORITY — if any reference image's brief describes an environment, location, or scene (e.g. 'park', 'street', 'café', 'jogging path', 'interior room', 'beach'), USE that environment as the background of the shot: place the subject INTO that scene with matching natural light, perspective, and depth of field. Do NOT default to studio when a location reference exists in the inputs. Only fall back to a neutral indoor/studio background when zero location/scene references exist upstream. No marketing language, no preamble — output the prompt only.
```

#### Auto-prompt image (multi-subject clause appended) — [prompt_synth.py:82-106](agent/flowboard/services/prompt_synth.py)
```
MULTI-SUBJECT MODE — CRITICAL: This shot contains MULTIPLE distinct people. The upstream context lists every reference image with a `ref_image_N` label. Compose ALL subjects into a single couple/group scene where every person appears in frame:
  • REFERENCE BY POSITION: name each subject by their `ref_image_N` label (e.g. 'ref_image_1 standing on the left, ref_image_2 on the right') so Flow can bind each person to the correct input image. NEVER replace `ref_image_N` with generic descriptors like 'an East Asian man'.
  • ARRANGEMENT: side-by-side, slightly turned toward each other, or natural couple/group composition. Every subject must be fully visible — no one cropped or hidden behind another.
  • POSE & GAZE rules apply to EACH subject — every face engages the camera; every expression neutral closed-mouth.
  • COMPLEMENTARY STANCES: each subject picks a DIFFERENT gesture from the stance pool — never repeat the same stance across subjects.
  • CONTACT: light natural couple-style contact is allowed (a hand on the other's shoulder, leaning slightly toward each other) but never invasive.
  • FRAMING: full upper-body or knees-up framing — wider than a single-subject shot — so all faces and any product stay in frame.
  • CHAR LIMIT: up to 400 chars for multi-subject scenes (overrides the 280 cap) since each subject needs description.
```

#### Auto-prompt video core — [prompt_synth.py:114-168](agent/flowboard/services/prompt_synth.py)
```
You are a video-motion prompt builder for an i2v pipeline (8-second clip, Veo-style). The source still is the first frame — describe what unfolds across the next 8 seconds.

INTENT FIRST. Look at the source: who is this person, what are they feeling, what would they naturally do in this moment? Let that drive the motion. The subject is a person with interiority, not a fashion model executing a pose pool.

ANTI-FREEZE (safety floor only): Veo locks onto frame 0 if the prompt is too passive. SOMETHING visible must change between frame 0 and frame 8 — but it can be as small as a half-blink, a weight shift, a gaze drifting to the lens and back, or fabric catching a breeze. What fails is adjective-only direction without a concrete change attached: 'gentle softness' alone freezes; 'a slight weight shift, eyes settling on the lens' doesn't.

PERFORMANCE notes — apply when they fit, ignore when they don't:
  • Match the energy of the source. A poised studio portrait wants a held gaze with a tiny weight shift, not a runway pose change. A walking street shot wants forward momentum.
  • Stillness is valid. A 6-second held moment with one small shift at the end can read more powerful than three beats of action stacked.
  • Don't pile gestures. One real motion that carries weight beats three checklist gestures.
  • Body language must read as in-character. The choice 'what does this person do next' should feel like THEIR choice, not the prompt-writer's.

STRUCTURE is free. Use time-coded beats (e.g. 0-3s / 3-6s / 6-8s) when the scene calls for sequenced action. Use a single continuous direction when the scene calls for sustained presence. Pick what fits — don't default to either.

ALWAYS include: natural blinks throughout, soft fabric and hair drift. These ground the clip without adding theatrical motion.

AUDIO — Veo generates sound, and that audio passes a content filter (`PUBLIC_MIRROR_AUDIO_FILTER`) that REJECTS the entire request when speech is generated over faces resembling real people. Most Flowboard scenes are portraits, so default hard to silent:
  • SILENT BY DEFAULT: no spoken dialogue, no voice-over, no lip-sync, no singing, no humming, no whispering. Mouths stay neutral closed-mouth.
  • SFX: only generic low-volume ambient cues that match the setting (room tone, fabric rustle, light footsteps, soft breeze). Keep it minimal — no effects-heavy soundscape.
  • MUSIC: optional soft restrained background — lo-fi, ambient pad, gentle piano — at low volume. Never lyrical, never a recognisable melody, never high-energy.
  • EXCEPTION: only when the user prompt EXPLICITLY asks for dialogue or singing should the clip include speech, and even then keep the audio direction generic (no specific accent / voice characteristic / impersonation) to keep filter risk low.

No scene cuts, no text overlays. Max 400 chars. Output the motion prompt only — no preamble.
```

Camera variants append one line (`_SYNTH_SYSTEM_VIDEO_DEFAULT` allows subtle dolly/pan; `_SYNTH_SYSTEM_VIDEO_STATIC` forbids zoom/pan/dolly), and `_MULTI_SUBJECT_VIDEO_CLAUSE` adds:
```
MULTI-SUBJECT MODE: The source frame contains MULTIPLE distinct people. Direct each subject independently — natural co-presence beats synchronized choreography:
  • Each subject performs their own motion. Don't force both/all to lean / turn / glance at the same time — that reads staged.
  • Subjects may acknowledge each other: a glance, a soft micro-smile (still closed-mouth), light contact (a hand drifting toward the other's shoulder, a slight lean toward each other). Or they may simply co-exist, each in their own moment. Both are valid.
  • ANTI-FREEZE applies PER SUBJECT: at minimum a blink or subtle shift for every person between frame 0 and frame 8. No one frozen while another moves.
  • REFERENCE BY POSITION: when directing actions, name each subject by their `ref_image_N` label (e.g. 'ref_image_1 turns slightly toward ref_image_2; ref_image_2 holds her gaze on the lens'). Never replace `ref_image_N` with generic descriptors.
  • Char limit bumps to 540 for multi-subject — each person needs their own direction.
```

#### Batch mode suffix — [prompt_synth.py:475-486](agent/flowboard/services/prompt_synth.py)
```
BATCH MODE: Output a JSON ARRAY of EXACTLY {count} distinct prompts. Each prompt MUST pick a DIFFERENT stance from the pool — no two variants may share the same gesture. Output ONLY the JSON array, no preamble, no markdown fences. Each prompt still respects the GAZE rule (face engages camera) and the char cap. Example:
[
  "Editorial photo, …, both hands in pockets, …",
  "Editorial photo, …, hand-on-hip three-quarter, …",
  …
]
```

#### Storyboard mode suffix — [prompt_synth.py:494-529](agent/flowboard/services/prompt_synth.py)
```
STORYBOARD MODE: Output ONE JSON OBJECT with exactly these keys:
  "prompts": array of EXACTLY {count} strings (≤280 chars each),
                each describing one beat of a continuous narrative —
                index 0 is the first beat, index {count}-1 the last.
  "parents": array of EXACTLY {count} entries, each null OR an integer.
                parents[k] = null  → beat k is a NEW SCENE/ROOT (will be
                  generated fresh — use ONLY when location/subject/visual
                  context legitimately changes from the prior beat).
                parents[k] = j (0 ≤ j < k) → beat k VISUALLY CONTINUES
                  from beat j — same room, same wardrobe, same framing
                  carry-over. The image will be EDITED from beat j's
                  output, so beat k's prompt MUST describe ONLY THE DELTA
                  (e.g. "now opens the package", "now wearing the shirt")
                  — DO NOT re-describe identity, room, lighting.
                Constraints: parents[0] MUST be null; parents[k] < k.
Coherence rules (every beat):
  • SAME subject identity across the whole sequence — anchor on
    `ref_image_1` if a person reference exists.
  • SAME products/wardrobe wherever the narrative places them.
  • Consistent lighting + colour palette within a continuity chain.
Per-beat:
  • photoreal editorial shot, GAZE engages camera, neutral closed-mouth.
  • each beat advances the story; no two beats interchangeable.
{narrative_seed_block}
Output ONLY the JSON object — no preamble, no markdown fences. Example:
{
  "prompts": [
    "Editorial photo, woman in living room, hands empty, neutral pose, …",
    "Same scene, woman now holds sealed brown package on lap…",
    "Same scene, woman opens package, blue jacket emerging from tissue…",
    "Same scene, woman tries on the blue jacket…"
  ],
  "parents": [null, 0, 1, 2]
}
```

#### Planner — [services/planner.py:39-70](agent/flowboard/services/planner.py)
```
You are the Flowboard planner.

Flowboard is a personal infinite-canvas workspace for AI media workflows.
Nodes are typed cards: `character`, `image`, `video`, `prompt`, `note`.
Edges express "use as reference".

When the user describes intent, you:
1. Respond conversationally in one or two short sentences.
2. If (and only if) the intent implies creating nodes, append a pipeline plan
   at the end of your message wrapped in a fenced JSON block:

```json
{
  "nodes": [
    {"tmp_id": "a", "type": "image", "params": {"prompt": "…"}}
  ],
  "edges": [
    {"from": "a", "to": "b", "kind": "ref"}
  ],
  "layout_hint": "left_to_right"
}
```

Rules:
- `tmp_id` is a short local alias you invent (used only to wire edges).
- `type` must be one of character / image / video / prompt / note.
- Edge `from` / `to` are `tmp_id`s OR `#shortId` of existing nodes.
- Prefer small plans (<= 6 nodes). Do NOT emit a plan if the user is just
  chatting.
- Never emit prose inside the JSON block.
- If no plan is appropriate, omit the JSON block entirely.
```

**All prompts are deeply tuned to fashion / e-commerce / studio-or-street single-subject photoreal editorial photography**. They will need full rewrites for anime, not edits — different style vocab, different motion conventions, no GAZE/closed-mouth constraints (anime expressions are non-photoreal), different anti-freeze rules.

---

## H. Auto-prompt Synthesis

### `aiBrief` lifecycle
- **Where stored**: `Node.data.aiBrief` (string) in the `node` table's JSON column.
- **When created**: lazily, after a media tile is generated or uploaded — `requestAutoBrief(rfId, mediaId)` in [frontend/src/api/autoBrief.ts](frontend/src/api/autoBrief.ts) (fire-and-forget). It POSTs `/api/vision/describe`, awaits text, then `patchNode(dbId, { data: { aiBrief: text }})`.
- **When updated on regen**: when a node's media changes (new generation / new upload), the patch in [NodeCard.tsx:81-93](frontend/src/canvas/NodeCard.tsx) sends `{aiBrief: null}` to **clear** the brief (the merge handler at [routes/nodes.py:95-104](agent/flowboard/routes/nodes.py) treats null as the delete-key sentinel). A fresh `requestAutoBrief` is then fired against the new media. So: cleared + re-derived on every regen, **not** re-derived in lockstep with the gen request — there's a tiny window where the new tile is rendered but the brief is empty.
- **When read**: only by `prompt_synth._collect_upstream` ([prompt_synth.py:281-282](agent/flowboard/services/prompt_synth.py)). Note the **prompt-first rule** at [prompt_synth.py:284-287](agent/flowboard/services/prompt_synth.py): the node's user-typed (or auto-composed) `prompt` always wins over `aiBrief`; the brief is only the fallback for upload-only nodes that never received a prompt.

### `/api/prompt/auto` flow
[prompt_synth.auto_prompt()](agent/flowboard/services/prompt_synth.py):
1. `_collect_upstream(node_id)` walks the edge graph **one hop only** (target_id == node_id ordered by `Edge.id`). For each immediate upstream node, it records the type, shortId, `ref_index` (positional slot in the dispatched `ref_media_ids[]` — only assigned if the node is a ref-source-type AND has media), the brief/prompt/title, and — for `image` nodes only — the shortIds of their **character grandparents** for multi-subject detection.
   - There is **NO recursive DFS**. Multi-hop chains don't accumulate text; only image-nodes look one extra hop up to find their character source. See [prompt_synth.py:285-295](agent/flowboard/services/prompt_synth.py).
2. `_distinct_subjects(records)` enumerates distinct character shortIds and switches to multi-subject system prompts when count ≥ 2.
3. `_format_user_message` renders the upstream context as a "Subject(s): … / Product / wardrobe: … / Reference image(s): … / Direction: …" block, labeling each ref-source by its **positional `ref_image_N`** — comment at [prompt_synth.py:372-374](agent/flowboard/services/prompt_synth.py) explains why: literal `#shortId` tokens in the prompt correlate with `PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED` false positives from Google's content filter.
4. `run_llm("auto_prompt", user_msg, system_prompt=…, timeout=90)`.

### `/api/prompt/auto-batch` flow
`auto_prompt_batch(node_id, count)`:
- Same upstream collection.
- Same system prompt, but appends the BATCH MODE suffix instructing the LLM to output a **JSON array of N pose-distinct prompts**.
- Single LLM call returns N variants; backend parses, fences-strip, pad/trim to count, returns to frontend.
- The frontend then passes those N prompts back as `params.prompts[]` on the worker request, which the worker turns into per-variant `structuredPrompt.parts[0].text` in [flow_sdk.gen_image](agent/flowboard/services/flow_sdk.py:619-642) (one Flow request item per variant).

### Scene-type detection logic
**There is NO regex / keyword "studio / street / cafe / beach" detection.** It exists entirely in the LLM's hands. The prompt teaches Claude/Gemini to **infer the role of each upstream image from its brief** (location vs person vs garment) and place the subject INTO any environment reference. See `_format_user_message`'s "ROLE INFERENCE" hint ([prompt_synth.py:432-440](agent/flowboard/services/prompt_synth.py)) which is appended only when 2+ image refs are present.

For **video** the camera handling is hardcoded via two system-prompt variants (`_SYNTH_SYSTEM_VIDEO_STATIC` vs `_SYNTH_SYSTEM_VIDEO_DEFAULT`) selected by an optional `camera` arg passed from the dialog. No automatic detection from upstream context.

### Pose-distinct rotation logic
There is no Python rotation code. The 8-stance pool is **listed verbatim in the system prompt** ([prompt_synth.py:46-54](agent/flowboard/services/prompt_synth.py)). The LLM is told to "rotate so generations stay diverse" and BATCH MODE explicitly says "Each prompt MUST pick a DIFFERENT stance from the pool — no two variants may share the same gesture". Diversity is enforced by the LLM, not by deterministic code.

---

## I. Frontend State Management

### Zustand stores

| Store | File | Key state | Key actions | Consumed by |
|---|---|---|---|---|
| `useBoardStore` | [store/board.ts](frontend/src/store/board.ts) (687 LOC) | `boardId, boardName, boards[], nodes[], edges[], loading, error` | `loadInitialBoard, refreshBoardState, refreshBoardList, switchBoard, createNewBoard, deleteBoardById, renameBoard, addNodeOfType, addReferenceNode, persistNodePosition, deleteNodeByRfId, addEdgeFromConnection, deleteEdgeByRfId, cloneNodeWithUpstream, updateNodeData, updateEdgeData, setNodes, setEdges` | Everywhere |
| `useGenerationStore` | [store/generation.ts](frontend/src/store/generation.ts) (936 LOC) | `active{requestId,timerId}, openDialog, openViewer, projectId, paygateTier, error` | `openGenerationDialog, openResultViewer, ensureProjectId, dispatchGeneration, refineImage, dispatchStoryboard, retryStoryboardShot, cancelGeneration` | Board, NodeCard, GenerationDialog, ResultViewer |
| `useChatStore` | [store/chat.ts](frontend/src/store/chat.ts) | chat messages cache, mentions parsing | send/load/etc | ChatSidebar |
| `usePipelineStore` | [store/pipeline.ts](frontend/src/store/pipeline.ts) | `activeRun, pollTimer, error` | `startRun, stopPolling` (1500ms poll) | ChatSidebar after plan accept |
| `useReferencesStore` | [store/references.ts](frontend/src/store/references.ts) | `items[], loading, error, panelOpen, query` | `load, save, remove, rename, togglePin, setQuery, togglePanel, setPanelOpen` | ReferencesPanel, NodeCard ★ Save |
| `useSettingsStore` | [store/settings.ts](frontend/src/store/settings.ts) | `imageModel, videoQuality` (localStorage-persisted) | `setImageModel, setVideoQuality` | GenerationDialog, dispatch path |

### React Flow setup

[Board.tsx:23-31](frontend/src/canvas/Board.tsx) registers **one** custom node component for every kind:
```ts
const nodeTypes = {
  character: NodeCard,
  image: NodeCard,
  video: NodeCard,
  prompt: NodeCard,
  note: NodeCard,
  visual_asset: NodeCard,
  Storyboard: NodeCard,
};
```
`NodeCard` is a 1651-LOC mega-component that switches on `data.type` internally. Single edge type ([VariantEdge](frontend/src/canvas/VariantEdge.tsx)) renders the bezier line + a `v{N}` chip when `data.sourceVariantIdx` is set.

Drop-add popover ([Board.tsx:51-104](frontend/src/canvas/Board.tsx)): when a connection drag ends on empty canvas, a 2-option popover (Image / Video) appears at the cursor. Clicking creates the node + auto-wires the edge from the source handle. The bigger AddNodePalette ([AddNodePalette.tsx](frontend/src/canvas/AddNodePalette.tsx)) is a top-of-canvas chip strip with all 7 node types.

Connection radius is bumped to 32px ([Board.tsx:335-337](frontend/src/canvas/Board.tsx)). Delete key + Backspace both delete selected nodes/edges.

### WebSocket subscription
There is **no in-browser WebSocket**. The vite proxy has a stub `/ws → ws://localhost:8101/ws` in [vite.config.ts:17-20](frontend/vite.config.ts), but `grep -rn 'new WebSocket'` across `frontend/src/` returns zero hits, and the agent has no `/ws` route. The UI polls REST:
- Per-request: `useGenerationStore.scheduleNextPoll` polls `GET /api/requests/{id}` until status terminates (with a `MAX_NETWORK_RETRIES=8` ceiling).
- Per-pipeline-run: `usePipelineStore` polls `GET /api/pipeline-runs/{run_id}` every 1500 ms.
- Activity feed: `useActivityFeed.ts` (didn't read in depth) polls `/api/activity`.

### Variant display in ResultViewer
ResultViewer ([components/ResultViewer.tsx](frontend/src/components/ResultViewer.tsx), 669 LOC, not fully read) reads `data.mediaIds[]` + `data.slotErrors[]`. Each variant tile renders blocked ones (slot_errors[i] non-null) with an inline error reason instead of falling through to the previous variant. "New variant +" calls `useBoardStore.cloneNodeWithUpstream(rfId)` ([store/board.ts](frontend/src/store/board.ts) — see Section J for the gist) which spawns a sibling node of the same type with the same upstream edges so the user can re-gen with fresh prompts but same refs.

Variant edge pinning: clicking a variant on a multi-variant upstream node patches the downstream edge's `source_variant_idx` (via `PATCH /api/edges/{id}`), so the next dispatch from that downstream consumes the pinned variant.

---

## J. Asset Handling

### Upload flow
[POST /api/upload](agent/flowboard/routes/upload.py) (multipart):

1. Pydantic validates `project_id` (UUID-ish regex `^[A-Za-z0-9_-]{1,128}$`).
2. MIME allowlist: `image/jpeg`, `image/png`, `image/webp`, `image/gif`.
3. Read bytes with `MAX_UPLOAD_BYTES + 1` cap (10 MB).
4. Magic-byte sniff (`_sniff_image_mime`) — defense-in-depth against lying Content-Type.
5. Dimension sniff (`_sniff_image_dimensions`) — extracts WxH from PNG/JPEG/WebP/GIF headers without Pillow.
6. `_classify_aspect(w, h)` → `IMAGE_ASPECT_RATIO_{SQUARE,PORTRAIT,LANDSCAPE}` with ±10% tolerance.
7. Base64-encode + `FlowSDK.upload_image(image_base64, mime_type, project_id, file_name)` → Flow's `/v1/flow/uploadImage` (via extension proxy).
8. Validate returned `media_id` shape.
9. Write bytes to `storage/media/{uuid}.{ext}` (the `{ext}` map: jpg/png/webp/gif/mp4/webm).
10. Upsert `Asset` row keyed by `uuid_media_id` UNIQUE — sets `local_path`, `mime`, optional `node_id`.

Return: `{media_id, mime, size, width?, height?, aspect_ratio?}`.

`POST /api/upload-url` does the same but server-side fetches the URL first; rejects non-public hosts via `_is_public_host` (SSRF guard) — see [upload.py:156-174](agent/flowboard/routes/upload.py).

### Generated media → persisted locally
- Image gen: response includes signed `flow-content.google` URLs at `data.media[].image.generatedImage.fifeUrl` — `media_service.ingest_urls()` upserts an Asset row with `url` only. Bytes are not fetched yet.
- First `GET /media/{media_id}` ([routes/media.py:22-41](agent/flowboard/routes/media.py)) checks the local cache; on miss it calls `media_service.fetch_and_cache(media_id)`, which:
  - Looks up the row, verifies `_url_allowed(url)` against `https://flow-content.google/`.
  - Fetches via `httpx.AsyncClient(timeout=30)`.
  - Whitelists returned content-type (`image/*` or `video/*`).
  - Writes bytes to `storage/media/{uuid}.{ext}`.
  - Updates Asset row's `local_path` and `mime`.
- Subsequent hits are FileResponse from disk; the URL's expiry doesn't matter once cached.

For workflow-mode video (Low-Priority Veo), the poll returns base64 MP4 directly on `/v1/media/{id}` — `ingest_inline_bytes` writes it to `storage/media/{uuid}.mp4` without ever touching a URL ([media.py:113-145](agent/flowboard/services/media.py)).

### Storage layout
`storage/` (configurable via `FLOWBOARD_STORAGE`):
- `storage/flowboard.db` — SQLite DB (348 KB in the working tree).
- `storage/media/` — flat directory of `{uuid_with_dashes}.{ext}`. **No project subfolders, no kind subfolders**. Sample: `31cc1426-fe96-460d-b5e3-36c34ebaebf1.jpg`, `8821a94d-…webp`, etc. (7 files in the working tree). The filename IS the media_id, which is globally unique across boards.

### Signed URL / local URL serving
Frontend never touches signed Flow URLs. `mediaUrl(mediaId)` in [api/client.ts:462-465](frontend/src/api/client.ts) returns `/media/{encoded(uuid)}` — that hits the local FastAPI route which serves from disk (or fetches on demand). The Vite proxy ([vite.config.ts:14-21](frontend/vite.config.ts)) forwards `/media` and `/api` to `localhost:8101`.

If the agent hasn't cached the bytes yet, `GET /media/{id}` returns 404 with `{available:false, has_url, reason}` so the UI can poll `/api/media/{id}/status` ([media.py:44-52](agent/flowboard/routes/media.py)).

---

## K. Authentication / Security

### HMAC between agent and extension
See Section F. 32-byte URL-safe random secret regenerated per-process; pushed over WS on connect; verified with `hmac.compare_digest` on every `/api/ext/callback` POST.

### Other auth
**None**. As documented in [README](README.md) the app is local-only single-user. The agent exposes `/api/*` on `127.0.0.1:8101` and the WS on `127.0.0.1:9223`. There is no session middleware, no JWT, no API key on the agent's HTTP surface. Any process that can reach loopback can hit `/api/boards` and do anything.

OpenAI API key (the one provider that takes a key) is stored at `~/.flowboard/secrets.json` with mode `0o600` ([services/llm/secrets.py:66-75](agent/flowboard/services/llm/secrets.py)) via atomic tmp+replace. No encryption — single-user, local-host. Same file holds `activeProviders` mapping.

### CORS
[main.py:78-84](agent/flowboard/main.py):
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```
**Wide-open CORS** combined with no auth means *any* webpage the user visits could (in principle) curl `/api/boards` via the user's browser. The mitigating assumption is that the agent only binds loopback and that the user runs nothing else hostile on the box.

### Local-only assumptions that complicate multi-machine
- `WS_HOST` boot guard refuses non-loopback ([main.py:22-26](agent/flowboard/main.py)).
- Extension hardcodes `ws://127.0.0.1:9223` and `http://127.0.0.1:8101/api/ext/callback` ([background.js:8-9](extension/background.js)).
- `~/.flowboard/secrets.json` is host-local.
- SQLite DB is host-local.
- Media cache is host-local.
- Frontend's `/api/*` and `/media/*` rely on the Vite proxy (or being served by the same agent in production); there's no `VITE_API_BASE` env var — calls are relative paths.
- `flow_client.callback_secret` is per-process — no persistence — fine for single-user, breaks for any HA setup.
- "Cancel a request" only works for queued rows — no IPC mechanism between processes if you scale the worker out.

---

## Step 3 — Gap Analysis for Target Use Case

| Target requirement | Status | Where |
|---|---|---|
| **Multi-project isolation** | 🟡 Partial | Boards already form independent containers (each has its own `nodes`/`edges`/`chatmessage`/`plan`/`pipelinerun` rows scoped by `board_id`), and `BoardFlowProject` enforces 1:1 to a remote Flow project. BUT: `Reference` library is **global** (`source_board_id` is nullable) — see [models.py:79-99](agent/flowboard/db/models.py). `Asset` rows are NOT board-scoped (only optional `node_id`) — every uploaded image is keyed only by media_id. Cross-board leakage is purely a UI-discipline thing today; the schema makes it possible to attach any media_id to any node on any board. |
| **Project → Scene → Shot hierarchy** | 🟡 Partial | The current model is **two levels**: Board → Node. A `Storyboard` node fakes a third level inside `Node.data.shots[]`, with `parentShotIdx` forming a tree — but this lives inside one JSON blob, not as separate Node rows, and is capped at 8 shots ([routes/prompt.py:60](agent/flowboard/routes/prompt.py)). Target's Project→Scene→Shot is **three** distinct entities; the existing Storyboard tree is closer to "Scene" than to the full hierarchy. |
| **Project Bible per project** | ❌ Not in Flowboard | No table or JSON column holds project-wide style/palette/lighting metadata. No injection hook in the prompt-synth path. The system prompts are hardcoded literals in [prompt_synth.py:29-200](agent/flowboard/services/prompt_synth.py) — no per-project override mechanism. |
| **Scene Bible (spatial layout text + master establishing shot ref)** | ❌ Not in Flowboard | No scene entity. Master-shot-as-ref is not a concept; refs are wired via Edge rows ad-hoc per Node. |
| **Script-to-shot breakdown (Vietnamese)** | 🟡 Partial | `auto_prompt_storyboard` does take a `narrative_seed` free-text and produces N beats with parents ([prompt_synth.py:532-645](agent/flowboard/services/prompt_synth.py)). LLM is language-agnostic so VN seeds should work, but the system prompt is English and assumes editorial fashion vocabulary. Capped at 8 beats; not designed for top-down script parsing. |
| **Per-shot workflow chain (script → still → approval → motion → video → output)** | 🟡 Partial | The chain is buildable today by hand: script-text in a `prompt` node → `image` node (still) → user reviews → `video` node (motion). The "approval gate" is implicit — user simply clicks Generate on the next node. There's no enforced pause/approval step. Storyboard handles the still side; video isn't tied into the storyboard subgraph. |
| **Multi-shot consistency mechanisms** | 🟡 Partial | (a) Master establishing shot per scene: not modeled; could be approximated by setting an edge from a fixed image node to every downstream. (b) Character identity chain: works today via edges — each downstream gen takes upstream `mediaId`s as `IMAGE_INPUT_TYPE_REFERENCE`. (c) Lighting/time conventions from Scene Bible: ❌ no Scene Bible. |
| **Approval gates between still gen and video gen** | ❌ Not in Flowboard | The plan/pipelinerun system runs continuously to completion. No pause/approval state — `PipelineRun.status` is `pending|running|done|failed`. To add this, we'd need to introduce an "awaiting_approval" intermediate status and a frontend approval UI. |
| **Multiple video providers (Dreamina/Seedance/Flow)** | ❌ Not in Flowboard | `gen_video` is hardcoded to Flow's `batchAsyncGenerateVideoStartImage` ([flow_sdk.py:301-401](agent/flowboard/services/flow_sdk.py)). Video model resolution is a `VIDEO_MODEL_KEYS[tier][quality][aspect]` table for Veo only. No provider abstraction layer for video (the LLM abstraction layer is a good pattern to mirror — see [services/llm/](agent/flowboard/services/llm/)). |
| **Multiple still providers (Flow/Flux/Seedream)** | ❌ Not in Flowboard | `gen_image` + `edit_image` are hardcoded to Flow's `batchGenerateImages`. Two checkpoints (`GEM_PIX_2`, `NARWHAL`) are selectable, but both are Flow models. No abstraction. |
| **Multiple LLM providers (Claude CLI / Claude API / OpenAI API)** | ✅ Already in Flowboard | Three providers: Claude CLI, Gemini CLI, OpenAI (dual-mode: codex CLI OR OpenAI API). Registry in [services/llm/registry.py](agent/flowboard/services/llm/registry.py) maps each feature (auto_prompt/vision/planner) to one provider via user setting. Adding a Claude API path means adding a fourth `LLMProvider` class — the abstraction holds. |
| **Scene composition (ffmpeg concat)** | ❌ Not in Flowboard | No ffmpeg dependency, no concat job. The agent has no media-mux capabilities. |
| **Cost tracking per shot / scene / project** | ❌ Not in Flowboard | Flow's `/v1/credits` is fetched for the user's overall balance ([flow_client.py:125-190](agent/flowboard/services/flow_client.py)), but per-request cost is never recorded. `Request.result` carries no cost field; activity log doesn't track it. Veo per-request cost is implicitly known (model-key → credits) but never stored. |
| **Asset library with filters** | 🟡 Partial | `Reference` table + `/api/references?q=` exists with text-search on label/ai_brief, `pinned`/`position` fields, JSON `tags[]`, idempotent save-by-media_id, drag-to-canvas spawn. **But**: no kind filter parameter on the endpoint (it just returns everything and the frontend filters locally), no project filter, no date filter, no pagination. Tags exist but no tag-write UI is visible in the brief survey. |
| **Provider-agnostic asset references** | ❌ Not in Flowboard | All refs are Flow `media_id` strings — they only work as IMAGE_INPUT_TYPE_REFERENCE inside Flow gen calls. Cross-provider use would need a normalization layer (e.g. "give me bytes / base64 / signed URL of asset X") which the codebase has the foundation for via `media_service.cached_path` + `fetch_and_cache` but doesn't expose as a provider-facing API. |

---

## Step 4 — Risk Register

| # | Risk | Where |
|---|---|---|
| R1 | **Flow assumed as the only video provider, deeply.** `flow_client`, `flow_sdk`, `_handle_gen_video`, captcha bridge, paygate tier resolution, video poll loop, signed-URL/CDN ingestion — all built around Flow. Substituting Dreamina/Seedance means a new provider class **plus** breaking the "every gen goes through the extension/captcha" assumption baked into [worker/processor.py](agent/flowboard/worker/processor.py) and the upload flow ([routes/upload.py:177-236](agent/flowboard/routes/upload.py)). |
| R2 | **E-commerce / fashion vocabulary in node-type semantics.** Specifically `visual_asset` is a "product / wardrobe" reference ([prompt_synth.py:421](agent/flowboard/services/prompt_synth.py)); `character` + `visual_asset` → "image" is the canonical "model + product" combo. For anime this maps poorly (you want character + environment + pose-direction, not character + product). Renames aren't enough — the prompt-synth role inference rules ([prompt_synth.py:432-440](agent/flowboard/services/prompt_synth.py)) embed the e-commerce mental model. |
| R3 | **System prompts are fashion-specific and not parameterized.** All five system prompts in [prompt_synth.py:29-210](agent/flowboard/services/prompt_synth.py) plus `_VISION_SYSTEM` in [vision.py:35-44](agent/flowboard/services/vision.py) hard-code "editorial fashion photography", "closed-mouth", "GAZE engages camera", an 8-stance pool, and audio constraints specific to Veo i2v + real-person filter. These need to be **rewritten for anime** (different style vocab, expressive faces are good not bad, different anti-freeze concerns, no real-person filter), and they need to become **per-project overridable** to support the Project Bible. The current code has no override seam — all prompts are module-level constants. |
| R4 | **Tight coupling: prompt-synth ↔ Claude CLI is loose (good); prompt-synth ↔ Flow's `ref_image_N` slot semantics is tight (problematic).** The `ref_image_N` numbering exists because Flow binds inputs positionally. A different image provider would have different binding semantics (e.g. Flux uses ControlNet conditioning). The "reference by position" instructions in [prompt_synth.py:88-91](agent/flowboard/services/prompt_synth.py) leak Flow's contract into the prompt. |
| R5 | **Test coverage gaps for modules we'd touch.** 29 test files in [agent/tests/](agent/tests/) cover routes, services, worker, prompt synth, providers, validators. **No frontend tests at all** (no vitest/jest/playwright/cypress). The frontend is the surface most exposed to a port to a new node model. Worker has `test_processor_tier_fallback.py` + `test_storyboard_worker.py` but no test for `_handle_gen_video` polling, no test for inline-bytes ingestion. Extension JS has zero tests. |
| R6 | **SQLite-specific assumptions.** `PRAGMA foreign_keys=ON` ([session.py:18](agent/flowboard/db/session.py)) and one runtime `ALTER TABLE` migration ([session.py:45-51](agent/flowboard/db/session.py)). No `json_extract` / `->>` SQLite-only operators in the codebase — JSON queries are done in Python after fetching the row. **Postgres migration is mostly cosmetic** (swap connect-string, swap JSON→JSONB, swap the boot-time ALTER TABLE for proper Alembic migrations). But: the runtime ALTER TABLE pattern itself is fragile and won't survive Alembic adoption. |
| R7 | **Local-only assumptions everywhere.** WS host-check hard-refuses non-loopback ([main.py:22-26](agent/flowboard/main.py)); extension hardcodes loopback URLs ([background.js:8-9](extension/background.js)); CORS is `*` ([main.py:78-84](agent/flowboard/main.py)); no auth on `/api/*` at all. Going multi-user/multi-machine means: introduce auth, scope CORS, parameterise extension URL, ditch the unauthed WS or auth it. |
| R8 | **Single-board UI assumption is partially soft.** `useBoardStore` already manages a `boards[]` list and `switchBoard(id)` — see [store/board.ts:255-405](frontend/src/store/board.ts) — but `useGenerationStore.projectId` and the per-node poll table are not invalidated on board switch (the comment at [store/board.ts:198](frontend/src/store/board.ts) says "reset poll-state on the generation store" but I didn't verify that the switchBoard path actually does this). Look for stale-state-on-switch bugs when scaling. The ProjectSidebar component already exists ([components/ProjectSidebar.tsx](frontend/src/components/ProjectSidebar.tsx), 342 LOC). |
| R9 | **In-process worker queue with no persistence.** On agent restart, queued rows stay `queued` forever (no recovery scan for queued rows; only `running` rows are touched — see [main.py:32-49](agent/flowboard/main.py)). For long anime projects with many shots, this is fragile. Needs swap to durable queue (or persistence-based recovery on boot). |
| R10 | **`Storyboard` capitalisation mismatch.** Frontend uses `"Storyboard"`; backend `NodeType` Literal lists only lowercase types ([routes/nodes.py:13](agent/flowboard/routes/nodes.py)). Either nodes.py is bypassed, or `Storyboard` nodes are created some other way. **Needs verification — see Section L** — and adapting the node-type taxonomy without resolving this risks duplicating the bug. |
| R11 | **Prompt-first vs aiBrief rule.** [prompt_synth.py:284-287](agent/flowboard/services/prompt_synth.py) makes `prompt` win over `aiBrief`. For anime-style continuity (where you'd want a vision-derived character description to persist across regens) this rule may need inversion or per-project override. |
| R12 | **System-prompt sizes / token cost.** Multi-subject + image system prompt + ref text + ROLE INFERENCE clause can run ~3 KB. Multiply by 8 storyboard beats and per-shot batch calls. Cost-tracking absence (R-13 below in gap) means budget regressions ship invisibly. |

---

## Step 5 — Reuse Plan

| Concern | Recommendation | Rationale |
|---|---|---|
| Frontend canvas (React Flow setup) | **Keep as-is** | `Board.tsx` + edge/handle/drop-popover setup is generic and not coupled to e-commerce semantics. The only e-commerce-flavored thing is which node types appear in the palette — that's data, not code. |
| Node component library (NodeCard, handles, status badge) | **Refactor** | The single 1651-LOC `NodeCard` switches on `data.type` internally; adding new node kinds (Scene, Shot) inflates the file further. Split into per-type sub-components with a shared shell, then add anime-specific kinds (`scene`, `shot`, `script`, `bible`) alongside existing ones. Status badge + handles + drag-drop affordances are reusable as-is. |
| Backend API structure (FastAPI routes layout) | **Extend** | The `routes/{boards,nodes,edges,…}.py` layout is clean. Add `routes/projects.py` (already exists for Flow project binding — keep that and add domain Project semantics), `routes/scenes.py`, `routes/shots.py`, `routes/bibles.py`. CORSMiddleware + lifespan setup stay. |
| Database schema | **Refactor** | Existing `Board` ≈ Project, but lacks Bibles/Scenes/Shots and lacks proper project scoping on `Asset` / `Reference`. Introduce: rename Board→Project (or layer Project on top), add Scene + Shot tables, add ProjectBible/SceneBible JSON tables, add `project_id` FK to Asset + Reference. Keep Node + Edge as the per-Shot workflow graph. Migrate via Alembic at the same time (replaces the runtime ALTER TABLE pattern). |
| Worker queue mechanism | **Refactor** | In-process asyncio.Queue with no persistence won't survive longer anime projects + scene-render jobs (R9). Either move to a durable queue (Redis/RQ, or a SQL-backed queue scanned on boot), or at minimum add a "scan queued rows on startup" recovery pass. The handler signature `async (params) → (result, error)` is solid — keep it. |
| Google Flow extension bridge | **Keep as-is** (when Flow is the chosen video provider) | The captcha+WS+HMAC bridge is the novel IP and works well. Becomes "one provider's transport" inside a wider abstraction. Don't touch unless you're rewriting Flow integration. |
| Claude CLI subprocess pattern | **Extend** | Working subprocess pattern with stdin-piping for Windows-safety, JSON envelope, vision attachments. Adding a Claude API path is a new `LLMProvider` class — keep the CLI provider as one option. |
| Auto-prompt synthesis system | **Refactor** | The framework (collect upstream, format user msg, pick system prompt, run LLM, parse) is reusable. But: (a) make system prompts loaded from Project Bible + provider-agnostic templates, not module constants; (b) generalize `_REF_SOURCE_TYPES` and `ref_image_N` labeling to a per-provider strategy; (c) replace fashion-specific vocab with anime conventions. |
| Asset storage + signed URL handling | **Extend** | `storage/media/{uuid}.{ext}` flat layout + on-demand fetch + provider-allowlisted URLs is sound. Extend with `project_id` scoping (sub-directories or DB-level filter) and add provider-format adapters (URL refresh / base64 conversion / bytes for direct upload). |
| Zustand store organization | **Extend** | Per-domain stores (board, generation, chat, pipeline, references, settings) is the right factoring. Add `useProjectStore`, `useScenesStore`, `useScriptStore`, `useBibleStore`. `useGenerationStore` becomes broader (or splits per dispatch type). |
| Project sidebar / multi-board UI | **Refactor** | `ProjectSidebar.tsx` already exists for multi-board switching — it's the foundation for multi-project. Refactor to make Project (Bible + Scenes + Shots) the unit shown in the sidebar; current "Board = flat canvas" semantic becomes "Shot's workflow graph". |

---

## Section L — Open Questions

These are things I genuinely couldn't determine from code alone. Please confirm before planning kicks off:

1. **`Storyboard` capitalisation mismatch.** [routes/nodes.py:13](agent/flowboard/routes/nodes.py) lists `NodeType = Literal["character","image","video","prompt","note","visual_asset"]` (no `"Storyboard"`), but [frontend/src/api/client.ts:113](frontend/src/api/client.ts) + [AddNodePalette.tsx:14](frontend/src/canvas/AddNodePalette.tsx) include `"Storyboard"` and the worker has `_handle_gen_storyboard`. How does the frontend successfully create a Storyboard node — does Pydantic actually accept the capitalised value, or is there a separate creation path I missed?
2. **`PlanRevision` table.** The schema defines it but no route or service writes to it that I found. Is it a stub for a planned feature, or am I missing the writer?
3. **`aiBrief` re-derivation timing.** On regen, the frontend clears `aiBrief` via `{aiBrief: null}` then fires `requestAutoBrief` fire-and-forget. There's a window where the new media is rendered but no brief is on the node. If a downstream gen fires in that window, it sees no brief and falls back to the typed prompt — is that intentional, or is there meant to be a write barrier?
4. **Activity log writes.** The activity feed lists `auto_prompt`/`vision`/`planner` as types, written via `services/activity.record_activity`. Are those rows in `request` table (and the worker just doesn't process them) — confirming via test? I inferred this from `routes/activity.py` reading `Request` rows but didn't see the writer.
5. **Worker queue durability across restarts.** `running` rows get flipped to `failed` on boot. `queued` rows do not — they remain `queued` and the in-memory queue lost them. Is the intent that the user re-clicks Generate, or is there a recovery sweep I missed?
6. **`/api/auth/me` `paygate_tier_unknown` behavior.** The new "fail loud when tier is unknown" policy ([processor.py:92-94](agent/flowboard/worker/processor.py)) — does the frontend retry automatically once the extension catches up, or does the user have to dismiss + retry?
7. **CORS `allow_origins=["*"]` posture.** Is this intentional for local dev convenience, or is the intent to lock it down to `http://localhost:5173` + the production frontend host?
8. **`POST /api/boards/{id}/project` idempotency under race.** Two parallel clicks both call `ensure_board_project`. The second-write guard at [routes/projects.py:73-75](agent/flowboard/routes/projects.py) returns the existing row — but the FIRST call still made a Flow `createProject` round-trip. Is there a way to prevent the duplicate remote project creation, or is this acceptable because the UI shouldn't double-fire?
9. **Reference deletion vs asset retention.** Deleting a `Reference` row leaves the cache file in place per [references.py:181-194](agent/flowboard/routes/references.py). Over time `storage/media/` will accumulate orphans (no node + no reference). Is there a GC sweep planned, or is the intent that disk is cheap?
10. **`flow_project_id` lifecycle on local board delete.** [routes/boards.py:80-83](agent/flowboard/routes/boards.py) explicitly says it does NOT delete the remote Flow project. For anime adaptation: do we want a "delete remote too" option, and does Flow's API even support it?
11. **System prompt language.** All prompts are English. The user is Vietnamese and target scripts are Vietnamese. Does the LLM handle VN→English bridging natively (likely yes for Claude/Gemini), or should the prompts be bilingual / Vietnamese?
12. **`Storyboard` shot cap (1..8).** Worker enforces 1..8 ([processor.py:479](agent/flowboard/worker/processor.py)). For anime scenes with 20+ shots, is this cap a deliberate Flow-cost guardrail or arbitrary? Lifting it requires rethinking the "all-in-one storyboard node" UX since 20+ tiles inside one card won't read well.
