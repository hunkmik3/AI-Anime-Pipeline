# Reskin pipeline — source motion + target designs

This connects the accepted square/period recipe and the X-Ray crowded-party 3D
trial to Giant Studio's durable production controller. It does not certify every
source or style. The approved trial contains generated-output findings that stay
in its original reports; integration does not clear them.

## Use in the app

1. On Canvas, choose **Phim hoàn chỉnh** and **Reskin bám chuyển động gốc**.
   Choose the target style, output ratio and 480p/720p resolution, then upload the source.
   Leave **Chốt tạo hình chính trước** on to customize designs before production.
2. Analysis builds source cuts, dialogue, speakers and the asset catalog. A bounded
   original-video/audio pass reviews each interval with its neighboring cuts.
   Unclear content or mismatches stop the handoff before material/image/video
   generation. Machine observations are evidence, not a 100% fidelity claim.
3. In **Tạo hình chính**, upload or generate character/environment sheets. Optional
   prop and crowd cards are available here too. **Thiết kế thay thế** changes an
   asset's target appearance; it keeps the source asset ID, function and custody.
   The source catalog/shotlist is not rewritten to describe a different plot.
   A replacement also needs an appropriate uploaded or newly generated sheet.
   Character states use the chosen master; individually uploaded state sheets stay.
4. Choose **Chốt sheet & tiếp tục**. Each clip creates a local white-model motion
   reference, one staging still, the cinematic GPT/Avis prompt, then its video.
   No per-shot images, separate raccord planner or previous-generated-frame chain
   are introduced by this route. Image atlases pack material references if needed.
5. **Pipeline Reskin** shows source findings, the references and run controls.
   Use **Toàn bộ clip · chọn phạm vi** to run selected clips. **Material + prompt**
   stops before video; the render option generates and assembles the selected set.
   Individual legacy canvas gen/write buttons direct Reskin boards to this panel.
6. Audio is listened to without the expected script. If music is present/uncertain,
   Demucs extracts voices; a source interval without dialogue can become silent.
   Native clips are preserved. Foley/room tone may be reduced and separation can
   leave artifacts. A separate output check examines dialogue and sampled visuals;
   residual or unknown findings are never automatically accepted.
7. Download the final assembly if checks pass. If all clips generated but checks
   found issues, an **unapproved preview** can still be viewed/downloaded with the
   findings shown. No automatic video regeneration occurs.

Prompts retain the mandatory dialogue compliance header and
`NO BACKGROUND MUSIC. NO BGM. NO SCORE.` Audio remains enabled for dialogue.
A prompt rule alone cannot guarantee the model will obey it.

## Three independent ratios

- White-model video: original source frame geometry, no soundtrack; only even-pixel
  padding for the encoder. Depth inference respects portrait/landscape geometry.
- Staging image: **one continuous 21:9 scene**, not panels or a replacement camera.
- Generated/assembled video: **1:1, 9:16 or 16:9**, chosen by the owner. The provider
  receives that ratio directly. Assembly fits/pads rather than cropping action.
  New edges synthesized when ratios differ still require visual review.

Each motion reference is limited to 15 seconds. Clips split at actual source cuts;
a single shot/sentence that cannot fit safely blocks preparation rather than being
silently truncated. Full generated takes are retained by default to avoid clipping
speech, so the final duration may exceed the source duration.

## Source corrections and recovery

The source report includes source bytes, shotlist digest, per-shot observations,
heard dialogue and explicit limitations. It checks cut boundaries, camera/framing,
action/positions, identity/wardrobe, environments/props and dialogue delivery.
Words, speaker IDs and delivery must agree; utterance timing is bounded to the
selected interval (not a claimed phoneme/lip-sync measurement).

For a specific finding, inspect the original in Pipeline Reskin. Advanced shotlist
editing preserves fields not edited. After saving corrections, explicitly request
**Đối chiếu bản sửa**. Identical intervals reuse receipts; changed intervals are
read again. Unknown charged attempts retain their journal and require provider
reconciliation; a button click does not silently repeat the charge. There is no
one-click override that falsely marks a source finding approved.

Server-owned source receipts cannot be forged/cleared by canvas autosave. Source
edits invalidate review; target design/style/ratio edits leave the source review
intact but invalidate affected production inputs. A changed source catalog also
invalidates its dependent reading. Motion references bind source hash, cuts and
review receipt, while design images bind independently.

Closing the tab does not cancel jobs. Matching completed jobs are reused across
runs. A provider submission with no certain result remains unknown; resume polls
the original video ID. Explicit run resume can retry failed local preparations;
image/video generations are not automatically resubmitted. Local/audio workers
write partial files then validate before promotion. Audio listening checkpoints
still prevent paid retries during local recovery. PostgreSQL and storage must be
kept with the application; do not move just the Git checkout and expect media or
provider receipts to follow it.

## Server setup (macOS / Linux / Windows)

Install the normal agent/frontend dependencies and PostgreSQL as described in the
repository's setup documentation. FFmpeg and ffprobe must be on PATH. Configure
Avis, Atrium (source audiovisual review) and R2 in the server's existing config.
No credentials belong in Git. `GET /api/automation/reskin/capabilities` reports
readiness without returning secrets; new Reskin jobs check it before generation.

Depth inference uses the existing ONNX/OpenCV/NumPy dependencies. On first use it
downloads Depth Anything V2 Small ONNX and OpenCV YuNet to the Hugging Face cache.
For an offline machine set `FLOWBOARD_DEPTH_MODEL` and `FLOWBOARD_YUNET_MODEL` to
local model files. The result is shaded relative depth with smoothed detected
faces, **not a reconstructed 3D scene or an anonymity guarantee**.

Demucs needs a compatible PyTorch runtime. Recommended isolated Python 3.11 setup
from the repository root (works in PowerShell with `uv` installed):

```sh
uv venv --python 3.11 agent/.venv-audio
```

macOS/Linux:

```sh
uv pip install --python agent/.venv-audio/bin/python "demucs==4.0.1" "torch==2.6.0" "torchaudio==2.6.0" "soundfile>=0.12"
```

Windows PowerShell:

```powershell
uv pip install --python agent/.venv-audio/Scripts/python.exe "demucs==4.0.1" "torch==2.6.0" "torchaudio==2.6.0" "soundfile>=0.12"
```

The agent finds this environment automatically; `FLOWBOARD_AUDIO_PYTHON` overrides
its executable path. Alternatively install the agent's `[audio]` extra in a
compatible Python environment. Demucs downloads its htdemucs weights on first use.
The API server and durable worker must use the updated code; the frontend must be
rebuilt for the new tab. Setup checks do not prove provider credit, quota or uptime.

## Verification of this integration

- Regression and new tests use a disposable PostgreSQL database and mock providers;
  no new paid generation was used for integration verification.
- Controller cases cover all three output ratios, motion plus ordered KYC sheets,
  one staging still, no unwanted frame/planner path, audio cleanup, quality hold,
  preview assembly, matching-job reuse, and refused unknown submissions.
- Source cases cover missing observations, all six unresolved criteria, source
  version changes, wrong words/speaker/delivery/timing, and cached failed attempts.
- Actual local worker smoke: 180×320 and 320×180 sources retain those dimensions,
  30fps and no audio. A four-second source excerpt passes the isolated Demucs
  worker. These are technical checks, not new aesthetic approval of full films.
- Frontend build and UI interaction check verify that changing output to 9:16
  leaves the staging label at 21:9 and motion at source ratio.

Provider quality remains probabilistic. Review the generated cut and sound before
handoff, particularly changed aspect ratios, crowded scenes, fast action, and
newly mapped objects. Anime 2D is an available preset, not a newly validated visual
trial from this integration.

## Unattended source preparation and notifications

Source jobs keep accepted catalog drafts separately from request receipts. A
resume validates retained groups locally and reads only groups that still need
work; it does not reset the source analysis or approve source fidelity. Historical
identity references are budgeted around complete current-shot frames. Known
malformed JSON receives one checkpointed correction. Explicit HTTP 429 responses
are queued with backoff (up to eight waits), without requiring an open browser.

Concrete speaker-binding contradictions can receive one focused check against
original video and audio, with verbatim-word and evidence checks. The default
budget is 24 audiovisual rechecks per source (`FLOWBOARD_CATALOG_AV_REPAIR_LIMIT`);
`FLOWBOARD_CATALOG_AV_REPAIR=0` disables new ones. Receipts preserve corrections,
neighboring audio doubts and unresolved findings. Refused or unknown submissions
are retained by source interval and are not sent again through another model.
Independent later sequences may proceed, but an incomplete source catalog cannot
enter material/video production. Full source review remains a separate gate.

The header's **Thông báo** inbox shows persistent completion or actionable holds
across projects and suppresses stale failures while a resumed run is active.
Browser notifications require the user's browser permission and an open app tab;
there is no background Web Push service. Processing and stored outcomes survive
closing the tab. Manual pauses remain quiet.

Audiovisual recovery excerpts use content-addressed R2 keys: two local files
named `original-context.mp4` cannot share a URL unless their bytes match. The
published bytes are still checked before any review request. Saved reviewer
answers may identify findings by exact shot/code instead of numeric index; this
is accepted only when unambiguous and backed by the answer's own timed speech
observation. No words, times or resolved statuses are invented by that adapter.

## Full-film reading with a selected production prefix

When the owner limits generation (for example, the first 180 seconds), the
one-pass source worker retains the complete source analysis in the database and
prepares a separate production view of complete shots ending within that limit.
It reuses accepted catalog groups and original identity evidence from the full
film, without waiting for unrelated catalog failures later in the source.
Full-film findings and progress remain in their original checkpoints; the
selected view and its metadata live under `production-prefix/<input-key>/`.
Source/media, selected catalog, style rules and compiler changes invalidate the
derived preparation. Recovery preserves existing custom designs.

The selected view is only an observed candidate. Original audiovisual review is
still required before material or video generation, with all six source criteria,
exact dialogue and neighboring-cut context. Independent review excerpts run with
three concurrent requests (hard maximum four), retaining successful receipts even
when another excerpt fails. Unknown/refused requests are not resubmitted.
Catalog completion and audiovisual source readiness are reported separately.
The complete-clip limit is checked again when production starts.
