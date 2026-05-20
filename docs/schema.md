# Database schema

Phase 1 reshape. Postgres-only, Alembic-managed. The hierarchy is
**Project → Scene → Shot**; everything else hangs off Shot.

## ERD (text)

```
project
  ├── scene (project_id)
  │     └── shot (scene_id)
  │           ├── node (shot_id)
  │           │     ├── edge (source_id, target_id)
  │           │     └── request (node_id)
  │           ├── plan (shot_id)             — transitional, replaced by Phase 7
  │           │     ├── planrevision (plan_id)
  │           │     └── pipelinerun (plan_id)
  │           └── reference (source_shot_id) — optional provenance
  ├── asset (project_id, node_id)
  ├── reference (project_id)
  ├── chatmessage (project_id)
  └── project_flow_mapping (project_id, PK)
```

Cross-cutting back-references:

- `scene.master_establishing_asset_id → asset.id` (set after the master shot
  renders; `ON DELETE SET NULL`).
- `shot.current_node_id → node.id` (workflow pointer; `SET NULL` on delete).
- `shot.final_video_asset_id → asset.id` (composed scene input; `SET NULL`).

## Tables

### `project`
| column | type | notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `name` | text | user-visible |
| `project_bible` | JSONB | art style, palette, line, lighting, negatives |
| `settings` | JSONB | default video provider, etc. |
| `created_at` | timestamptz | |

### `scene`
| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `project_id` | UUID FK | `ON DELETE CASCADE` |
| `name` | text | |
| `order_index` | int | per-project ordering |
| `scene_bible_text` | text | spatial anchor + master shot rules |
| `master_establishing_asset_id` | int FK | `asset.id`, nullable |
| `created_at` | timestamptz | |

Indexes: `(project_id)`, `(project_id, order_index)`.

### `shot`
| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `scene_id` | UUID FK | `ON DELETE CASCADE` |
| `order_index` | int | per-scene ordering |
| `script_text` | text | VN narrative of the shot |
| `status` | text | `idle / running / awaiting_approval / done / error` |
| `current_node_id` | int FK | nullable; points at the node where execution is paused |
| `final_video_asset_id` | int FK | nullable; populated when ffmpeg compose happens |
| `workflow_metadata` | JSONB | React Flow viewport, layout hints |
| `created_at` | timestamptz | |

Indexes: `(scene_id)`, `(scene_id, order_index)`.

### `node`
Per-shot workflow graph node (int PK preserved for frontend compatibility).
| column | type | notes |
|---|---|---|
| `id` | int PK | autoincrement |
| `shot_id` | UUID FK | `ON DELETE CASCADE` |
| `short_id` | text | unique per shot (Crockford-style 4-char) |
| `type` | text | character / image / video / prompt / note / visual_asset / Storyboard (Phase 4 adds: script / bible_ref / master_shot / approval_gate) |
| `x`, `y`, `w`, `h` | float | canvas coords |
| `data` | JSONB | type-specific payload |
| `status` | text | idle / queued / running / done / error |
| `created_at` | timestamptz | |

Indexes: `(shot_id)`, `(short_id)`, `UNIQUE(shot_id, short_id)`.

### `edge`
| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `shot_id` | UUID FK | `ON DELETE CASCADE` |
| `source_id`, `target_id` | int FK | `node.id`, `ON DELETE CASCADE` |
| `kind` | text | `ref` / `hint` |
| `source_variant_idx` | int | nullable; pins which `mediaIds[]` variant feeds downstream |

### `request`
Worker queue rows. Unchanged structurally from Phase 0.
| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `node_id` | int FK | nullable, `ON DELETE CASCADE` |
| `type` | text | gen_image / gen_video / proxy / create_project / gen_storyboard / etc. |
| `params` | JSONB | dispatch payload |
| `status` | text | queued / running / done / failed |
| `result` | JSONB | media_ids, op_errors, ... |
| `error` | text | top-level failure reason |
| `created_at`, `finished_at` | timestamptz | |

### `asset`
Auto-managed media cache index. `project_id` is the canonical ownership signal.
| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `project_id` | UUID FK | nullable until binding; `ON DELETE CASCADE` |
| `node_id` | int FK | nullable; `ON DELETE SET NULL` |
| `kind` | text | image / video / thumbnail / composed_scene |
| `uuid_media_id` | text UNIQUE | from Flow (or other provider) |
| `url` | text | latest signed GCS URL (expires) |
| `local_path` | text | cached file path |
| `mime` | text | |
| `asset_metadata` | JSONB | provider-specific extras |
| `created_at` | timestamptz | |

### `reference`
User-curated cross-project media library (saved variants for reuse).
| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `project_id` | UUID FK | nullable; `ON DELETE CASCADE` |
| `media_id` | text UNIQUE | the canonical handle |
| `url`, `label`, `kind`, `ai_brief`, `aspect_ratio`, `tags`, `pinned`, `position` | mixed | per-row metadata |
| `source_shot_id` | UUID FK | nullable; provenance, `ON DELETE SET NULL` |
| `source_node_short_id` | text | nullable; provenance short_id |
| `created_at` | timestamptz | |

### `chatmessage`
Per-project conversation log.
| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `project_id` | UUID FK | `ON DELETE CASCADE` |
| `role` | text | user / assistant / system |
| `content` | text | |
| `mentions` | JSONB | array of short_ids referenced |
| `created_at` | timestamptz | |

### `project_flow_mapping`
Renamed from `boardflowproject`. 1:1 with `project`.
| column | type | notes |
|---|---|---|
| `project_id` | UUID PK FK | `ON DELETE CASCADE` |
| `flow_project_id` | text | Google Flow project handle |
| `created_at` | timestamptz | |

### Transitional plan stack (Phase 7 replaces)

`plan(id PK, shot_id FK, spec JSONB, status, created_at)`
`planrevision(id PK, plan_id FK, rev_no, spec JSONB, edits JSONB, created_at)`
`pipelinerun(id PK, plan_id FK, status, started_at, finished_at, error)`

Kept transitionally so the chat→planner→Run flow keeps working until
Phase 7 reintroduces execution behind `ApprovalGateNode` and the
shot-status state machine.

## Phase 1 shim

`/api/boards/*` still routes through the old surface. Each "Board" is
created as `Project + Scene "Scene 1" + Shot`, and the returned `id` is
the Shot's UUID. This lets the existing extension/frontend keep working
unchanged until Phase 2 introduces `/api/projects`, `/api/scenes`,
`/api/shots`. The shim deletes when Phase 2 cuts over.

## Migrations

One revision so far:

- `86e7b45a4366` — initial anime schema (Phase 1 greenfield).

Run with `cd agent && alembic upgrade head`. Connection string defaults
to `postgresql+psycopg://flowboard:flowboard@localhost:15432/flowboard`
(matches `docker-compose.yml`); override via `FLOWBOARD_DATABASE_URL`.

## Tests

`pytest` uses a dedicated `flowboard_test` DB on the same container.
The conftest:

1. Auto-creates `flowboard_test` if missing.
2. Wipes `public` and runs `alembic upgrade head` once per session.
3. `TRUNCATE ... RESTART IDENTITY CASCADE` between every test.

This replaces the old SQLite `drop_all + create_all` per-test pattern.
