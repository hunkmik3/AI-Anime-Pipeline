# Studio Agent chat

Open **Chat Agent** on `/automation`. Each saved board has its own persistent conversation. The chat uses GPT through the existing Avis service (`AUTOMATION_ASSISTANT_MODEL`, default `gpt-6-luna`). Apply migrations before starting the backend: `cd agent && .venv/bin/alembic upgrade head`.

Supported requests include reading the current shotlist and profiles, editing image/video prompts, generating a specific existing material, writing a clip prompt, running selected clips through the existing production pipeline, changing aspect ratio/image size, and reading/pausing/resuming production jobs. Select a node in either canvas mode to refer to it as “mục này”. Upload a new source using **＋ Video nguồn**, which opens the established source/style workflow.

Examples:

- “Đọc thoại clip 1, chưa gen gì.”
- “Sửa prompt bối cảnh này: bỏ ghế, giữ kiến trúc và ánh sáng. Chưa gen ảnh.”
- “Tạo lại ảnh bối cảnh này.”
- “Viết prompt clip 2, chưa gen video.”
- “Gen clip 2–4 ở 480p và ghép lại.”

The assistant uses the same material dependencies, approved style sheets, cinematic prompt writer, source dialogue checks, and no-background-music rule as the canvas. It does not execute shell/SQL or inspect image pixels directly. It cannot invent/delete nodes, rewrite source shotlists, or force a new video take with identical inputs; production reuses matching outputs. Change the prompt before requesting a new take.

Each tool action records its arguments and outcome. Saving a prompt displays its before/after text and reloads the board. Generated work is tracked as a server job, with queued/running/failed/completed status distinct from the chat reply. Chat history is separate from board JSON; ordinary saves do not erase it.

A turn is bounded to eight tool steps. Mutations check current production inputs and board revision. The frontend temporarily disables canvas editing during a turn; other tabs changing inputs cause a conflict instead of a silent overwrite. Repeated delivery of the same message request key does not launch another turn. Stop prevents further tool dispatch after the current operation; already submitted generation continues. Server restart marks unfinished turns interrupted, preserving job receipts without resubmitting paid work.

Validation: `cd agent && .venv/bin/pytest -q tests/test_automation_assistant.py tests/test_production_run.py tests/test_automation_material_upload.py`; `cd frontend && npm run build`. Tests mock GPT and keep generation jobs queued; they do not incur generation charges.


## Primary sheets checkpoint

**Tạo hình chính** beside Canvas lists one identity sheet per character and one base sheet per environment, with profile, state list and clip usage. It is available on existing boards too. Upload replaces the canonical sheet through the existing public-image/media ingestion path; users can also edit the master prompt or generate only missing primary sheets.

New source-film uploads default to `review_masters: true`. The source analysis and board construction complete normally, then production is persisted as `paused` / `master_review`. No state/prop/prompt/video work is scheduled until **Chốt sheet & tiếp tục**. Disable **Chốt tạo hình chính trước** in the source form for uninterrupted production. Existing backend clients keep `ProductionRunConfig.review_masters=false` for compatibility; new UI and chat production requests enable the checkpoint. The continuation preserves the original run's clip scope, mode, settings and budgets.

A changed identity marks generated state sheets stale (`needsIdentityRefresh`), retains their files and ignores superseded job receipts. Production regenerates those states using the new identity reference. Explicit uploaded state overrides remain intact. Shot-package and frontend reference binding exclude stale generated states. Approval requires the latest board revision, all required primary references, valid source inputs and no unresolved/active generation. Generic resume and a second production run cannot skip this checkpoint.

Validation: `cd agent && .venv/bin/pytest -q tests/test_primary_materials.py tests/test_source_film.py tests/test_production_run.py tests/test_automation_material_upload.py tests/test_automation_assistant.py`. Source-film tests cover both uninterrupted and checkpointed handoffs through mocked final assembly. UI smoke uses a copied board and stops before approval; no paid media generation.


## Production progress

The **Tiến độ** strip above the canvas polls the read-only, owner-scoped `GET /api/automation/projects/{id}/progress` every four seconds. Click **Chi tiết** to open a floating, scrollable popover for source analysis (cuts, keyframes, speech, shotlist and profiles), master sheets, the primary-sheet approval checkpoint, variants/props, scene continuity, prompts, clips and assembly. The strip stays 35px high; new updates do not auto-expand it or move the canvas. Close details with ×, Escape, or a click outside. Advanced production controls are available inside **Cài đặt & công cụ**, removed from the main canvas. Job polling remains mounted independently of these controls.

Percentages represent successful work units divided by the full planned workload for that phase, including validated reused results. Unscheduled clips remain in the denominator. Provider calls with no intermediate measurement use an indeterminate bar; no ETA or time-based percentage is invented. Failed and unresolved jobs remain visible and do not count as complete. Master approval explicitly says **Chờ bạn chốt**. Closing/reloading the browser preserves server counters; older analyses can show only the counters originally recorded. Supporting reference jobs have a separate explicitly dynamic denominator.

Source callbacks persist per-step history, while production runs store a read-only progress plan alongside their existing task receipts. Polling never schedules work, changes a board, or contacts providers. Tests: `tests/test_production_progress.py` plus the existing production/source/master suites; frontend build and browser smoke use an existing paused test board without generating media.
