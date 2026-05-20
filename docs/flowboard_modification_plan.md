# Flowboard → Anime Narrative Pipeline — Modification Plan

## Decisions confirmed by user

| # | Decision | Choice |
|---|---|---|
| 1 | Hierarchy | **A** — Rename Board→Project, add Scene + Shot as separate tables |
| 2 | Video provider | **B** — Build abstraction supporting Flow + Dreamina |
| 3 | Database | **A** — Migrate to Postgres + Alembic from the start |
| 4 | NodeCard refactor | **A** — Split 1651 LOC component into per-type components |
| 5 | System prompt language | **Bilingual** — VN input accepted, EN system prompts, EN output for providers |

---

## Phase Overview

Six phases. Each phase has a clear deliverable and a stopping point for review.

| Phase | Theme | Duration | Deliverable |
|---|---|---|---|
| 0 | Pre-flight: verify, fork, baseline | 1-2 days | Forked repo, all tests passing, dev env up |
| 1 | Database refactor + Alembic + Postgres | 3-4 days | New schema migrated, old data optionally migrated |
| 2 | Backend API for Project/Scene/Shot hierarchy | 3-4 days | New routes, old `/api/boards` deprecated |
| 3 | Frontend hierarchy views | 4-5 days | ProjectDashboard, SceneView, ShotEditor (refactored canvas) |
| 4 | NodeCard refactor + new anime node types | 3-4 days | Per-type components, new nodes: `script`, `bible`, `master_shot` |
| 5 | Video provider abstraction + Dreamina integration | 4-5 days | `gen_video` routes through provider registry; Dreamina works |
| 6 | Anime-specific prompts + Project/Scene Bible injection | 2-3 days | New system prompts; Bible auto-injection in prompt_synth |
| 7 | Scene composition (ffmpeg) + approval gates | 2-3 days | Scene-level video concatenation; approval flow per shot |

**Total estimate: ~4 weeks of focused work.** Multiply by 1.5-2x for realistic calendar time with iteration.

---

## Phase 0 — Pre-flight (1-2 days)

### Goal
Forked repo set up locally, all original tests pass, you understand the codebase well enough to modify it.

### Tasks

**0.1 Fork and clone**
```bash
gh repo fork crisng95/flowboard --clone
cd flowboard
git checkout -b anime-adaptation
```

**0.2 Bring up the original Flowboard as-is**
- Install Chrome extension from `extension/`
- Get Google Flow Pro/Ultra account (or skip if not using Flow as video provider)
- Install Claude CLI for LLM provider
- `cd agent && python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- `cd frontend && npm install`
- Run agent + frontend, verify a test board renders

**0.3 Run all backend tests**
```bash
cd agent && .venv/bin/python -m pytest -q
# Should see 190 passed
```

**0.4 Resolve open question Q1 from analysis (Storyboard capitalisation bug)**
- Test: try `POST /api/nodes` with `type: "Storyboard"` via curl
- If it fails Pydantic validation → there's a separate creation path; find it
- If it succeeds → Pydantic Literal isn't enforcing; understand why
- Decision: either fix the Literal to include `"Storyboard"`, OR find the bypass and replicate it for new node types

**0.5 Document Dreamina API contract**
Before Phase 5, you need to know:
- Dreamina auth flow (Volcengine token? API key?)
- Submit endpoint shape (URL, headers, body)
- Polling pattern (sync? async with external_job_id?)
- Response format (URL? base64? signed URL expiry?)
- Multi-reference image input support
- First-frame + last-frame keyframe support

Write `docs/dreamina_api_contract.md` from manual testing with curl/httpie. This becomes input to Phase 5.

### Stop-point checklist
- [ ] Original Flowboard runs end-to-end (or at least non-Flow paths work)
- [ ] 190 tests pass
- [ ] Storyboard capitalisation behavior understood
- [ ] Dreamina API contract documented
- [ ] Personal Claude account configured for Claude CLI

---

## Phase 1 — Database refactor (3-4 days)

### Goal
Schema reshaped for Project/Scene/Shot hierarchy; Postgres + Alembic replacing SQLite + runtime ALTER.

### Tasks

**1.1 Add Alembic and Postgres dependencies**
```bash
# agent/requirements.txt additions
alembic>=1.13
asyncpg>=0.29
psycopg2-binary>=2.9  # for sync migrations
```

**1.2 Update Docker Compose to include Postgres**
Add `postgres:16` service, update `agent` env to use `postgresql+asyncpg://...`.

**1.3 Initialize Alembic**
```bash
cd agent
alembic init alembic
```
Configure `alembic.ini` and `env.py` to use SQLModel metadata + the new Postgres URL.

**1.4 Design new schema**

New tables (all use UUID PKs going forward):

| Table | Purpose | Key fields |
|---|---|---|
| `project` | Replaces `board`; top-level anime project | id, name, project_bible JSONB, settings JSONB, created_at |
| `scene` | New; ordered sequence of shots within a project | id, project_id (FK), name, order_index, scene_bible_text, master_establishing_asset_id (FK asset, nullable) |
| `shot` | New; one cinematic shot with its own workflow graph | id, scene_id (FK), order_index, script_text, status, current_node_id, final_video_asset_id (FK asset, nullable), workflow_metadata JSONB |
| `node` | Refactored; now scoped to a shot instead of a board | id, shot_id (FK, was board_id), short_id, type, x, y, w, h, data JSONB, status, created_at |
| `edge` | Refactored; scoped to a shot | id, shot_id (FK, was board_id), source_id, target_id, kind, source_variant_idx |
| `asset` | Add project_id FK so assets are project-scoped | id, project_id (FK, new), node_id (FK, nullable), kind, uuid_media_id, url, local_path, mime, metadata JSONB |
| `reference` | Add project_id FK to scope per-project | id, project_id (FK, new), media_id, label, kind, ai_brief, aspect_ratio, tags JSONB, pinned, position, source_shot_id, created_at |
| `chatmessage` | Re-scope to project (was board) | id, project_id (FK), role, content, mentions JSONB, created_at |
| `request` | Worker queue rows — unchanged structurally | (keep as-is) |
| `boardflowproject` | Rename to `project_flow_mapping` | project_id (PK), flow_project_id, created_at |

Tables to **drop** (no longer used):
- `plan`, `planrevision`, `pipelinerun` — replaced by per-shot workflow state in Phase 7 with approval gates

Tables to **keep unchanged**:
- `request`

**1.5 Write first migration**
- Generate baseline from new schema
- For migration from existing Flowboard data: write data-migration script that creates a default Project per old Board, a default Scene "Scene 1" per Project, a default Shot "Shot 1" per Scene, then re-parent all Nodes/Edges to that Shot
- If you have no existing data: skip the data migration, just create empty schema

**1.6 Migrate SQLAlchemy session config**
- Swap connect string to Postgres
- Change `JSON` column types to `JSONB` for query efficiency later (use `from sqlalchemy.dialects.postgresql import JSONB`)
- Remove the runtime ALTER TABLE in `session.py:45-51`
- Enable Postgres `pg_trgm` extension for asset library search later

**1.7 Update models.py**
Refactor all model classes per the new schema. Add UUID7-style PKs.

**1.8 Update existing tests for new schema**
Many tests reference `board_id` directly. Either:
- Create a fixture that builds Project → Scene → Shot → Nodes pyramid
- Use a `legacy_board()` helper that returns the default Shot, so old tests don't fully break

### Stop-point checklist
- [ ] `alembic upgrade head` runs clean on fresh Postgres
- [ ] All 190 original tests pass (adapted for new schema)
- [ ] No more SQLite-specific code or runtime ALTER TABLE
- [ ] Schema diagram updated in `docs/schema.md`

---

## Phase 2 — Backend API for new hierarchy (3-4 days)

### Goal
REST surface exposes Project / Scene / Shot CRUD. Old `/api/boards/*` still functional during transition but marked deprecated.

### Tasks

**2.1 New route files**
- `agent/flowboard/routes/projects.py` — replaces existing `boards.py` and the Flow-project subset of `projects.py`
- `agent/flowboard/routes/scenes.py` — new
- `agent/flowboard/routes/shots.py` — new
- `agent/flowboard/routes/bibles.py` — new (for Project Bible + Scene Bible CRUD)

**2.2 Endpoint inventory**

```
# Projects (top-level anime projects)
GET    /api/projects                    list
POST   /api/projects                    create  body: {name, project_bible?}
GET    /api/projects/{id}               detail with scene count, asset count
PATCH  /api/projects/{id}               update name, settings
DELETE /api/projects/{id}               cascade delete (scenes, shots, nodes, edges, assets)
GET    /api/projects/{id}/cost          aggregate cost across all shots

# Project Bible
GET    /api/projects/{id}/bible
PUT    /api/projects/{id}/bible         body: {art_style, color_palette[], line_style, lighting_conventions, negative_prompts[], style_anchor_asset_ids[]}

# Flow project binding (existing, just renamed)
GET    /api/projects/{id}/flow-project
POST   /api/projects/{id}/flow-project  idempotent bootstrap

# Scenes
GET    /api/projects/{project_id}/scenes        list ordered
POST   /api/projects/{project_id}/scenes        create  body: {name, order_index?, scene_bible_text?}
GET    /api/scenes/{id}                          detail with shot count
PATCH  /api/scenes/{id}                          update
DELETE /api/scenes/{id}                          cascade delete shots
POST   /api/scenes/{id}/reorder                  body: {shot_ids[]}
POST   /api/scenes/{id}/compose                  trigger ffmpeg concat (Phase 7)

# Scene Bible
GET    /api/scenes/{id}/bible
PUT    /api/scenes/{id}/bible                    body: {scene_bible_text, master_establishing_asset_id}

# Shots
GET    /api/scenes/{scene_id}/shots              list ordered
POST   /api/scenes/{scene_id}/shots              create  body: {order_index?, script_text?}
GET    /api/shots/{id}                           detail including workflow nodes+edges
PATCH  /api/shots/{id}                           update script, order, status
DELETE /api/shots/{id}                           cascade delete nodes
GET    /api/shots/{id}/workflow                  get nodes + edges
PUT    /api/shots/{id}/workflow                  save React Flow {nodes, edges}
POST   /api/shots/{id}/run                       start executing workflow
POST   /api/shots/{id}/cancel                    stop in-flight jobs
GET    /api/shots/{id}/jobs                      list request rows scoped to this shot

# Existing routes to update
PATCH  /api/nodes/{id}                           still works; nodes now belong to shots
POST   /api/nodes                                require shot_id in body, not board_id
... (similar for edges)

# Existing routes to deprecate (return 410 Gone after Phase 3)
/api/boards/*                                    
```

**2.3 Pydantic schemas**
Mirror tables in `schemas/`:
- `ProjectCreate`, `ProjectRead`, `ProjectUpdate`
- `SceneCreate`, `SceneRead`, ...
- `ShotCreate`, `ShotRead`, ...
- `ProjectBible`, `SceneBible`

**2.4 Service layer additions**
- `services/project_service.py` — project CRUD, bible CRUD
- `services/scene_service.py` — scene CRUD, reordering, composition trigger
- `services/shot_service.py` — shot CRUD, workflow execution dispatch

**2.5 Backward compatibility shim**
For 1-2 weeks, keep `/api/boards/*` working by aliasing `board_id` → default project's default scene's default shot. After frontend cuts over, remove.

### Stop-point checklist
- [ ] All new routes return correct shapes
- [ ] New tests covering Project/Scene/Shot CRUD
- [ ] Cascade delete works through 4 levels: project → scene → shot → node + edges
- [ ] Bible PUT validates JSON shape

---

## Phase 3 — Frontend hierarchy views (4-5 days)

### Goal
Three new views: ProjectDashboard, SceneView, ShotEditor (refactored canvas). Sidebar shows project list. URL routing introduced.

### Tasks

**3.1 Add React Router**
```bash
npm install react-router-dom@6
```

Routes:
```
/                                  → redirects to /projects
/projects                          → ProjectListPage
/projects/:projectId               → ProjectDashboard (scenes overview + bible editor)
/projects/:projectId/library       → AssetLibraryPage
/projects/:projectId/cost          → CostDashboard
/scenes/:sceneId                   → SceneView (shots list + scene bible editor)
/shots/:shotId                     → ShotEditor (the React Flow canvas, refactored)
```

**3.2 New stores**

`store/project.ts`:
```ts
useProjectStore: {
  projects: Project[],
  currentProjectId: string | null,
  projectBible: ProjectBible | null,
  loadProjects(), createProject(), switchProject(),
  updateBible(),
}
```

`store/scene.ts`:
```ts
useSceneStore: {
  scenes: Scene[],
  currentSceneId: string | null,
  sceneBible: SceneBible | null,
  loadScenes(projectId), createScene(), ...
}
```

`store/shot.ts`:
```ts
useShotStore: {
  shots: Shot[],
  currentShotId: string | null,
  loadShots(sceneId), createShot(), updateScriptText(), ...
}
```

Existing `useBoardStore` becomes `useShotWorkflowStore` (renamed; manages nodes/edges within current shot).

**3.3 New view: ProjectListPage**
- Grid of project cards
- Each card: thumbnail (latest scene's final frame), name, status, scene count, cost-to-date, last activity
- "Create new project" button → modal: name, optional clone from existing project

**3.4 New view: ProjectDashboard**
- Header: project name, edit button, cost meter, settings dropdown
- Left panel: Project Bible editor (form with all fields)
- Main area: Scenes grid — each scene shows order #, name, master shot thumbnail, shot count, status
- "Add scene" button
- Reorder via drag-and-drop

**3.5 New view: SceneView**
- Breadcrumb: Project / Scene
- Left panel: Scene Bible editor (text area for spatial map + master establishing shot picker)
- Main area: Shots list (ordered, with status badges, thumbnails, cost per shot)
- "Add shot" button
- Click a shot → navigate to `/shots/:id` (ShotEditor)

**3.6 Refactor existing canvas into ShotEditor**
- Move existing `Board.tsx` content into `routes/shot-editor.tsx`
- Header shows: breadcrumb (Project / Scene / Shot), shot order #, script text editor, run button, status, cost meter
- Right sidebar: settings for selected node
- Canvas: same React Flow setup as before, but scoped to one shot's workflow

**3.7 New: ScriptInput dialog**
- Available at SceneView level: "Generate shots from script"
- User pastes Vietnamese script
- LLM call to `/api/prompt/parse-script` (new endpoint, Phase 6) returns array of shot breakdowns
- User reviews, then bulk-creates shots with pre-populated script_text

**3.8 Update ProjectSidebar component**
Existing 342 LOC component — refactor to be project-aware. Show project list with "Create" / "Switch" actions.

**3.9 Asset library page**
- Filters: project (default current), type, tag, date range
- Search by label or aiBrief
- Tag editor
- Currently `useReferencesStore` filters locally — push filtering server-side for scale

### Stop-point checklist
- [ ] Can create a new project from UI
- [ ] Can add scenes to project, shots to scene
- [ ] ShotEditor works identically to old Board canvas
- [ ] URL routing handles deep links
- [ ] Project switching does not leak state across projects (test by switching mid-generation)

---

## Phase 4 — NodeCard refactor + new node types (3-4 days)

### Goal
1651 LOC `NodeCard.tsx` split into per-type files. Three new node types added for anime workflow.

### Tasks

**4.1 Identify per-type sections in current NodeCard**
Read through `NodeCard.tsx` and group the conditional branches by node type:
- character branch (~200 LOC)
- image branch (~300 LOC)
- video branch (~300 LOC)
- prompt branch (~150 LOC)
- note branch (~100 LOC)
- visual_asset branch (~250 LOC)
- Storyboard branch (~350 LOC)

**4.2 New folder structure**
```
frontend/src/canvas/nodes/
├── BaseNodeShell.tsx       # Status indicator, handles, drag affordance
├── CharacterNode.tsx
├── ImageNode.tsx
├── VideoNode.tsx
├── PromptNode.tsx
├── NoteNode.tsx
├── VisualAssetNode.tsx
├── StoryboardNode.tsx
├── ScriptNode.tsx          # NEW
├── BibleRefNode.tsx        # NEW
├── MasterShotNode.tsx      # NEW
└── ApprovalGateNode.tsx    # NEW
```

**4.3 New anime-specific node types**

**ScriptNode**
- Inputs: none (root)
- Outputs: `{ script_text: string }`
- Settings: multi-line textarea for VN script of the shot
- No worker task; data passthrough
- Replaces the implicit "script lives in prompt" pattern

**BibleRefNode** (Project Bible OR Scene Bible)
- Inputs: none (root)
- Outputs: `{ bible_text: string, type: 'project' | 'scene' }`
- Settings: dropdown to pick which bible (defaults to current scene's)
- No worker task; loads bible text from API into outputs

**MasterShotNode**
- Inputs: none (root)
- Outputs: `{ asset: Asset }`
- Settings: pick from scene's master establishing shot
- No worker task; loads the asset reference

**ApprovalGateNode**
- Inputs: any
- Outputs: passthrough of input
- Settings: title, optional notes
- Special: workflow engine pauses here, surfaces in approval queue

**4.4 Type registry update**
```ts
// frontend/src/api/client.ts
export type NodeType = 
  | "character" | "image" | "video" | "prompt" | "note" 
  | "visual_asset" | "Storyboard"
  | "script" | "bible_ref" | "master_shot" | "approval_gate";
```

Backend `routes/nodes.py`:
```python
NodeType = Literal[
    "character", "image", "video", "prompt", "note", 
    "visual_asset", "Storyboard",
    "script", "bible_ref", "master_shot", "approval_gate",
]
```

Resolve the capitalisation bug from Phase 0.4 here.

**4.5 AddNodePalette update**
Add the 4 new types to the palette, grouped:
- "Refs": character, visual_asset, master_shot, bible_ref
- "Generation": image, video
- "Logic": script, prompt, approval_gate
- "Misc": note, Storyboard

### Stop-point checklist
- [ ] All 7 original node types still work in their own files
- [ ] 4 new node types render and persist data
- [ ] No file exceeds 500 LOC
- [ ] ApprovalGateNode reaches "paused" state when workflow hits it

---

## Phase 5 — Video provider abstraction + Dreamina (4-5 days)

### Goal
`gen_video` task routes through a provider registry. Dreamina (Seedance) added as a second provider alongside Flow.

### Tasks

**5.1 Create video provider abstraction**

`agent/flowboard/services/video/base.py`:
```python
from typing import Protocol, TypedDict

class VideoGenSubmitParams(TypedDict):
    first_frame_url: str
    motion_prompt: str
    duration_seconds: int
    last_frame_url: str | None
    aspect_ratio: str
    quality: str | None

class VideoGenSubmitResult(TypedDict):
    external_job_id: str  # Provider-specific opaque ID

class VideoGenPollResult(TypedDict):
    status: str           # 'pending' | 'running' | 'success' | 'failed'
    video_bytes: bytes | None
    video_url: str | None
    error: str | None
    duration_seconds: float | None
    cost_usd: float

class VideoProvider(Protocol):
    name: str
    async def submit(self, params: VideoGenSubmitParams) -> VideoGenSubmitResult: ...
    async def poll(self, external_job_id: str) -> VideoGenPollResult: ...
```

**5.2 Wrap existing Flow code as `FlowVideoProvider`**

`agent/flowboard/services/video/flow.py`:
- Move `_handle_gen_video` logic from `worker/processor.py` here
- Adapt to the new Protocol interface
- Keep extension bridge as-is; only wrapping changes

**5.3 Implement `DreaminaVideoProvider`**

`agent/flowboard/services/video/dreamina.py`:
- HTTPX async client to Volcengine/Bytedance Dreamina API
- Submit → store external_job_id
- Poll endpoint
- Use the contract you documented in Phase 0.5

**5.4 Registry pattern (mirror LLM pattern)**

`agent/flowboard/services/video/registry.py`:
```python
_providers = {
    "flow": FlowVideoProvider,
    "dreamina": DreaminaVideoProvider,
}

def get_video_provider(name: str) -> VideoProvider:
    return _providers[name]()
```

**5.5 Per-project video provider preference**
- Add `default_video_provider` to Project.settings
- VideoNode settings has provider override
- Resolution order: node override → project default → global default ("flow")

**5.6 Worker rewrite**
`_handle_gen_video` becomes thin:
```python
provider_name = params.get("provider", "flow")
provider = get_video_provider(provider_name)
submit_result = await provider.submit(...)
# Schedule polling
...
```

**5.7 Settings UI**
- Project settings page: pick default video provider
- LLM provider settings already exist — model the UI on that

**5.8 Mirror for still image providers**
Same pattern: `services/image/{base.py, flow.py, flux.py, registry.py}`. Defer Flux implementation if you're not using it yet — leave a stub provider class with `NotImplementedError`.

### Stop-point checklist
- [ ] Generating a video with `provider: "flow"` works identically to before
- [ ] Generating a video with `provider: "dreamina"` works end-to-end
- [ ] Cost tracking written to `Request.result.cost_usd` for both providers
- [ ] Failure modes (timeout, content filter, auth failure) surface clearly

---

## Phase 6 — Anime prompts + Bible injection (2-3 days)

### Goal
System prompts rewritten for anime narrative. Project Bible + Scene Bible auto-injected into every prompt synthesis call.

### Tasks

**6.1 Rewrite system prompts**

Old prompts live in `services/prompt_synth.py:29-210`. They are 100% fashion editorial. Throw away and rewrite from scratch.

New prompts needed (file: `services/prompt_synth_anime.py`):

**`_ANIME_IMAGE_SYSTEM`**
- Style: cinematic 2D anime, cel-shaded
- Identity preservation rules (match character refs)
- Composition rules (rule of thirds, depth, camera angle vocabulary)
- Lighting rules (consistent with scene bible)
- Anti-blur, anti-3D, anti-realistic flags
- Variable bilingual handling: read VN script, output EN prompt

**`_ANIME_VIDEO_SYSTEM`** (motion prompt)
- Anime cadence: characters animate on threes, backgrounds on ones (cel animation principle)
- Time-coded beats for 4-8s clips
- Anti-freeze guidance
- Audio: silent by default (anime has post-production audio)
- Lip-sync: deferred — don't generate dialogue audio
- Variable bilingual

**`_ANIME_SCRIPT_PARSE_SYSTEM`** (new — script → shot breakdown)
- Input: VN script of full scene
- Output: structured JSON `{shots: [{order, script_text, camera_angle, characters_in_frame, environment}, ...]}`
- Used by `/api/prompt/parse-script` endpoint

**`_VISION_SYSTEM_ANIME`** (replace fashion vision)
- Describe anime characters/scenes for downstream prompt synthesis
- Note: identity markers, outfit, expression, pose, framing
- No "marketing language" concerns — this is for technical anime production

**6.2 Bible injection in prompt_synth**

Modify `_format_user_message` to load and prepend:
```
PROJECT BIBLE (style anchor):
{project.project_bible.art_style_description}
Palette: {project.project_bible.color_palette}
Line style: {project.project_bible.line_style}
Lighting: {project.project_bible.lighting_conventions}
Negative: {project.project_bible.negative_prompts}

SCENE BIBLE (spatial anchor):
{scene.scene_bible_text}
```

**6.3 Master shot reference auto-attachment**
When a shot's workflow includes a `MasterShotNode`, the still gen prompt automatically references it as `ref_image_0` (highest priority position).

**6.4 New endpoint: `/api/prompt/parse-script`**
- Body: `{scene_id, script_text}`
- LLM call with `_ANIME_SCRIPT_PARSE_SYSTEM`
- Returns shot breakdown
- Frontend uses this in ScriptInput dialog at SceneView level

**6.5 Per-project prompt overrides (advanced)**
Allow Project Bible to include `custom_image_system_prompt` and `custom_video_system_prompt` strings that override the defaults. Power users only.

**6.6 Bilingual handling**
- All system prompts: English
- User input (script_text, scene bible, project bible): user's language (Vietnamese default)
- LLM output to providers (Flow/Dreamina/Flux): English
- LLM responses back to user UI: same language as input
- No translation step needed — modern LLMs handle code-switching natively

### Stop-point checklist
- [ ] Generating a shot with Project Bible "warm noir office drama" + Scene Bible "rainy night, neon" produces images that visibly match
- [ ] Two shots in same scene produce visually consistent character + environment
- [ ] VN script input produces clean EN prompt output
- [ ] Old fashion prompts removed (or moved to `prompt_synth_legacy.py` if you want to keep a record)

---

## Phase 7 — Scene composition + approval gates (2-3 days)

### Goal
ffmpeg pipeline assembles approved shot videos into a scene video. Per-shot approval gates pause workflow until human review.

### Tasks

**7.1 Approval gate flow**

Workflow engine changes:
- When DAG execution hits `ApprovalGateNode`, mark `Shot.status = 'awaiting_approval'`, set `Shot.current_node_id = approval_gate_node_id`, return without proceeding
- Frontend polls shot status (or WebSocket — add WS in Phase 8 if needed)
- User views variants in ApprovalQueuePage
- User picks a variant, hits approve → `POST /api/jobs/{job_id}/approve`
- Backend resumes workflow from the approval gate

**7.2 Approval queue UI**
- Route: `/projects/:projectId/approvals`
- List of shots in `awaiting_approval` status across the project
- Each row: shot info, variant grid, approve/reject buttons
- Reject = mark for regen with notes

**7.3 ffmpeg integration**

Add `ffmpeg-python` to requirements.

`services/composition.py`:
```python
async def compose_scene(scene_id: UUID) -> Asset:
    shots = await get_shots_ordered(scene_id)
    video_paths = [shot.final_video_asset.local_path for shot in shots if shot.final_video_asset_id]
    if not video_paths:
        raise ValueError("No approved shot videos to compose")
    output_path = f"storage/composed/scene_{scene_id}.mp4"
    # ffmpeg concat
    import ffmpeg
    inputs = [ffmpeg.input(p) for p in video_paths]
    ffmpeg.concat(*inputs).output(output_path).run()
    return await create_asset(output_path, type='composed_scene')
```

Trigger: `POST /api/scenes/{id}/compose`

**7.4 SceneView "Compose" button**
- Visible when ≥1 shot has `final_video_asset_id` set
- Click → submit composition job → poll status
- On success: show composed video in inline player + download link

**7.5 Cost rollup**
- `Project.cost` = sum of all shots' cost
- `Scene.cost` = sum of its shots' cost
- `Shot.cost` = sum of its jobs' cost
- Update on every job completion via worker hook

### Stop-point checklist
- [ ] Full pipeline: VN script → parse to 3 shots → workflow each → approve each → compose → final mp4
- [ ] Approval queue surfaces all paused shots across all projects
- [ ] Composition handles missing shots gracefully (skip or fail with clear message)
- [ ] Cost rolls up correctly at all 3 levels

---

## Anti-patterns to avoid during implementation

1. **Don't keep both old and new schema simultaneously.** Migrate, delete old, move forward.
2. **Don't write code without running it.** Each task in each phase should produce a runnable artifact you've executed at least once.
3. **Don't refactor speculatively.** If a piece of original Flowboard code isn't blocking your phase, leave it.
4. **Don't skip Phase 0.5 (Dreamina API documentation).** Real API will differ from your assumptions. Documenting first saves rework in Phase 5.
5. **Don't merge phases.** Each phase has a stop-point review. Skipping the review compounds errors.

---

## After Phase 7 — Deferred to Phase 8+

Things explicitly out of scope for Phases 0-7 but worth tracking:

- WebSocket for live job status (currently polling)
- Persistent worker queue (currently in-process; lose queued jobs on restart)
- Multi-user authentication
- Audio generation (ElevenLabs voiceover + lip-sync post-processing)
- Consistency checker node (LLM vision compares new still to scene's previous stills)
- Variant rating + auto-selection
- Workflow templates (clone a shot's workflow to other shots)
- Project clone (start Season 2 from Season 1 structure)
- Production deployment (currently local-only)
- CORS lockdown + auth on `/api/*`

---

## Suggested execution order if budget is tight

If you can only do part of this:

**Minimum viable adaptation (1-2 weeks):**
- Phase 0 (must)
- Phase 6 only (rewrite prompts for anime, keep old Board structure)

This gives you anime-flavored Flowboard without the hierarchy refactor. Crude but functional.

**Recommended minimum (3 weeks):**
- Phase 0, 1, 2, 3 (full hierarchy + UI)
- Phase 6 (anime prompts)
- Skip 4, 5, 7

You get the Project/Scene/Shot structure with anime prompts; defer NodeCard refactor, Dreamina provider, and composition.

**Full plan (4-5 weeks):**
- All 7 phases as above.

---

## Handoff to Claude Code

Each phase should be a separate Claude Code session. For each phase:

1. Start fresh Claude Code session
2. Paste this plan
3. Tell it: "Implement Phase N. Read the goal, tasks, and stop-point checklist. Confirm understanding, then begin. Ask before making non-obvious design decisions."
4. Review at stop-point before moving to next phase

For Phase 0 in particular, the Dreamina API documentation step is **your** job (Claude Code can't access Dreamina's docs). Do that manually before Phase 5.
