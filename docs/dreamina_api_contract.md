# Dreamina (Seedance / BytePlus ARK) API Contract

> **Status**: filled in from live curl probes on 2026-05-21. Authoritative reference for the `DreaminaVideoProvider` implementation in Phase 5.
>
> All shapes below are observed against `seedance-1-5-pro-251215` on the ap-southeast-1 BytePlus endpoint. Raw evidence (one JSON per probe) lives in `docs/samples/raw/` (gitignored). Canonical, sanitized samples live in `docs/samples/`.

## 0. Pre-requisites

- [x] BytePlus ARK account with API access enabled (region ap-southeast-1)
- [x] API key issued (format: `ark-<uuid>-<suffix>`)
- [x] Working curl probes for submit, poll, and download (see `docs/samples/`)

## 1. Authentication

| Field | Value |
|---|---|
| Auth scheme | Bearer token in `Authorization` header |
| Required headers | `Authorization: Bearer <api_key>`, `Content-Type: application/json` |
| Credential rotation | On-demand from the BytePlus console — no expiry observed |
| Where to store on Flowboard side | `~/.flowboard/secrets.json` under `apiKeys.dreamina` (same pattern as the LLM providers — see `agent/flowboard/services/llm/secrets.py`) |

### Sample auth header

```
Authorization: Bearer ark-316d7cca-122d-4b27-82f1-2e75ea439e61-f895c
Content-Type: application/json
```

The API key prefix is `ark-` followed by a UUID and a short suffix. Malformed keys fail with HTTP 401 `AuthenticationError` / "The API key format is incorrect"; missing header fails with HTTP 401 / "the API key or AK/SK in the request is missing or invalid" (see `docs/samples/error_auth.json`).

## 2. Submit endpoint (start a video generation)

| Field | Value |
|---|---|
| Method | `POST` |
| URL | `https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks` |
| Required body fields | `model` (string), `content` (array of content blocks; ≥1 text block + ≥1 image_url block) |
| Optional body fields | `duration` (int seconds; default 5; tested values: 5, 8, 10) — see §2.3 for inline prompt flags |
| Response (200) | `{ "id": "cgt-<yyyymmddhhmmss>-<5char>" }` — only the task ID, nothing else |
| Failure modes | 400 BadRequest (missing/invalid params, unreachable image), 401 Unauthorized (auth), 404 NotFound (bad model) |

### 2.1 Content block types

Valid `content[].type` values (revealed by the API's own error message):

- `text` — the prompt. Exactly one expected.
- `image_url` — `{"url": "<https URL>"}`. Fetched by the API at submit time; bad URL → 400 synchronously.
- `audio_url` — not exercised; presumed available on audio-capable models.
- `video_url` — not exercised; presumed for video-conditioned generation.
- `draft_task` — not exercised; likely chains to a prior `draft: true` task.

Unknown types fail with HTTP 400 / `InvalidParameter` and the message enumerates the supported set.

### 2.2 Sample request body (working baseline)

```json
{
  "model": "seedance-1-5-pro-251215",
  "content": [
    {"type": "text", "text": "A girl turns her head and smiles, cinematic lighting"},
    {"type": "image_url", "image_url": {"url": "https://example.com/character.png"}}
  ]
}
```

### Sample response

```json
{"id": "cgt-20260521225953-zr48p"}
```

Task IDs are 28 chars: `cgt-` + 14-digit UTC timestamp + `-` + 5 lowercase alphanumerics.

### 2.3 Inline prompt flags (aspect ratio, resolution)

Aspect ratio and resolution are **not** top-level body fields. They are encoded as inline flags inside the `text` content block. Flags observed working:

| Flag | Example | Notes |
|---|---|---|
| `--rt <W:H>` | `--rt 16:9` | Aspect ratio. Tested values: `16:9`, `9:16`, `1:1`. Default: `1:1`. |
| `--rs <Np>` | `--rs 1080p` | Resolution tier. Tested: `720p`, `1080p`. Default: `720p`. |

The flags are stripped from the prompt before generation. The chosen values are echoed back in the poll response (`ratio`, `resolution`).

Example:

```json
{"type": "text", "text": "Wide cinematic shot of a mountain --rt 16:9 --rs 1080p"}
```

`duration` is the exception — passed as a top-level integer field, not an inline flag:

```json
{
  "model": "seedance-1-5-pro-251215",
  "duration": 8,
  "content": [ … ]
}
```

### 2.4 Image input handling

| Image format | Supported? | Notes |
|---|---|---|
| URL (`image_url.url`) | **Yes** | Must be publicly reachable over HTTPS. The API fetches at submit time; failure → HTTP 400 synchronously (`error_bad_image.json`). |
| Base64 inline | Not tested | The OpenAI-compatible image_url shape supports `data:` URLs; should work but not verified. |
| Pre-uploaded asset ID | Not in this API surface | TOS direct upload exists separately; not needed for the v3 contents/generations route. |

**Implication for Flowboard**: the `media_id` → local file at `storage/media/{uuid}.{ext}` needs to be exposed via a public URL before submit. Options:
- Upload to a temp bucket (S3/GCS/TOS) — needs an additional dependency.
- Expose Flowboard's existing `/media/{id}` route over a tunnel — needs a public hostname.
- Read the bytes and inline as `data:image/png;base64,<b64>` — try this first; if the API accepts it on this model, no hosting needed.

Image size caps and dimension limits were not probed (`docs/samples/raw/` images are 1024×1024 picsum).

### 2.5 Multi-reference image support (CHARACTER ANCHOR — NOT SUPPORTED on this model)

| Question | Answer |
|---|---|
| Can a single submit attach N reference images for character/style anchoring? | **No** on `seedance-1-5-pro-251215`. |
| Why? | The API auto-classifies `role: "reference_image"` requests as `task_type=r2v` (reference-to-video). This model is `task_type=i2v` only. See `docs/samples/error_multiref_unsupported.json`. |
| Can multiple `image_url` blocks be sent at all? | Yes, but only with `role: "first_frame"` + `role: "last_frame"` for keyframe interpolation. See §2.6. |
| Workaround for Flowboard | (a) Use a different Seedance model variant that supports r2v (out of scope for Phase 5 — needs a probe). (b) Composite multiple references into one image upstream and submit as a single image_url. |

If multiple `image_url` blocks are sent without `role`, the API rejects with HTTP 400 / `InvalidParameter` / "role must be specified for image contents".

### 2.6 Keyframe interpolation (first_frame + last_frame)

Supported. Two image_url blocks, each tagged with `role`:

```json
{
  "model": "seedance-1-5-pro-251215",
  "content": [
    {"type": "text", "text": "Smooth cinematic transition between two scenes"},
    {"type": "image_url", "image_url": {"url": "https://example.com/frame_start.png"}, "role": "first_frame"},
    {"type": "image_url", "image_url": {"url": "https://example.com/frame_end.png"}, "role": "last_frame"}
  ]
}
```

Valid role values on this model: `first_frame`, `last_frame`. Other strings (`reference`, `reference_image`) are rejected; see `docs/samples/error_invalid_role.json` and `docs/samples/error_multiref_unsupported.json`.

For a single-image submit (the common case), `role` is **optional and should be omitted** — the API treats the lone image as the first_frame by default.

## 3. Poll endpoint (check progress)

| Field | Value |
|---|---|
| Method | `GET` |
| URL | `https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks/{task_id}` |
| Auth | Same `Authorization: Bearer …` as submit |
| Status enum (observed) | `running`, `succeeded`. Not observed but documented elsewhere: `queued`, `failed`, `cancelled`. |
| Typical poll interval | 15–30 s. Polling more often is wasteful (status moves running → succeeded in one step). |
| Total generation duration | 90–220 s wall clock. Faster when the queue is empty; longer (~3.5 min) with 3+ concurrent jobs. |
| Task TTL | `execution_expires_after: 172800` (48 h). After this, the poll returns HTTP 404 `ResourceNotFound`. |
| Unknown task ID | HTTP 404 / `ResourceNotFound` (see `docs/samples/error_task_not_found.json`). |

### Common response envelope fields

Every poll returns these regardless of status:

```
id                       string  — same as submit response
model                    string  — full model ID
status                   string  — see enum above
created_at               int     — unix seconds, submit time
updated_at               int     — unix seconds, last state change
service_tier             string  — observed "default"
execution_expires_after  int     — seconds; 172800 (48h)
generate_audio           bool    — default true (audio IS generated unless overridden — overrride param not tested)
draft                    bool    — observed false
priority                 int     — observed 0
```

### Sample poll response (running)

See `docs/samples/poll_running.json`.

### Sample poll response (succeeded)

See `docs/samples/poll_succeeded.json`. Adds these fields:

```
content.video_url        string  — signed TOS URL (see §4)
usage.completion_tokens  int     — billable units (see §5)
usage.total_tokens       int     — same as completion_tokens for this model
seed                     int     — randomly assigned per task
resolution               string  — "720p" | "1080p"
ratio                    string  — "1:1" | "16:9" | "9:16"
duration                 int     — seconds, 5 | 8 | 10
framespersecond          int     — 24 in all observations
```

### Sample poll response (failure / content filter)

**Not reproducible from a single safe-ish "sharp weapon" prompt** — the Seedance content filter is mild and accepts most prompts that would be rejected by stricter providers. No failure shape was captured in this round of probes. The `failed` envelope likely includes an `error` field analogous to the submit error shape; this needs to be verified when a real failure is hit in production. **Action for Phase 5**: instrument the failure path defensively (log the entire poll response on `status != "succeeded"`).

## 4. Output retrieval

| Question | Answer |
|---|---|
| Does the success response inline the video bytes? | No. URL only, in `content.video_url`. |
| Is the URL signed? Expiry? | Yes, TOS4-HMAC-SHA256 signature. `X-Tos-Expires=86400` (24 h). |
| Domain (for the Flowboard URL allowlist) | `ark-content-generation-ap-southeast-1.tos-ap-southeast-1.volces.com` |
| Video container/codec | MP4 / H.264 Constrained Baseline + AAC LC stereo 44.1 kHz |
| Pixel format | yuv420p, progressive |
| Bitrate | ~17.5 Mbps observed (5 s 720p 1:1 = 11 MB file) |
| Duration of the clip | Matches request: 5 / 8 / 10 s. Actual demuxed duration is +0.04 s due to encoder rounding. |
| Resolution × ratio matrix | `720p × 1:1` → 960×960, `1080p × 16:9` → 1920×1080. (Other combinations not enumerated; conservative formula: 720p ≈ 921,600 px/frame, 1080p ≈ 2,073,600 px/frame, adjusted to the requested ratio.) |
| **File lifecycle** | **The TOS file itself is deleted 24 h after creation** (`x-tos-expiration: rule-id="24h文件自动删除"`). The signed URL expiry and the file lifecycle align — there is no way to refresh either. Flowboard MUST download and persist locally on first success, not lazily on demand. |

Download is plain GET against the signed URL with no extra headers. Range requests are supported.

## 5. Cost / billing

| Question | Answer |
|---|---|
| Cost reported in response? | Yes — `usage.completion_tokens` (= `usage.total_tokens`). |
| Cost formula | `tokens ≈ pixels_per_frame × fps × duration_seconds ÷ ~1015`. Empirically: 720p 1:1 (960²) × 24 × 5 ≈ **108,900 tokens**; 1080p 16:9 × 24 × 5 ≈ **245,025 tokens**; 720p 1:1 × 24 × 8 ≈ **173,700 tokens**; 720p 1:1 × 24 × 10 ≈ **216,900 tokens**. The per-second rate at 720p 1:1 24fps is ≈ **21,700 tokens/sec**. |
| USD price per 1M tokens | **TBD** — look up on the BytePlus console under Pricing. The model + token-count math gives us cost; the per-token rate is the only missing piece. |
| Free tier / monthly quota | Not probed. No quota headers were exposed on any response. |

For Flowboard's `Request.result.cost_usd`: compute as `(completion_tokens / 1_000_000) * USD_PER_M_TOKENS`, with `USD_PER_M_TOKENS` configured per model in `~/.flowboard/config.json` (default to the observed Seedance 1.5 Pro rate once confirmed).

## 6. Rate limits

| Limit type | Value |
|---|---|
| Requests per minute | Not hit at 8 parallel error-tests + 4 parallel real submits + 6 parallel real submits (one burst of 3 was specifically a rate-limit probe). No 429 observed. |
| Concurrent jobs | At least 6 concurrent `running` tasks on a single key. Wall-clock latency rises (~100s → ~220s) under concurrent load. |
| Per-day cap | Not probed. |
| Rate-limit response headers | **None exposed.** The only ARK-provided response header is `x-request-id` (use for support tickets). |

**Recommendation for Phase 5**: implement exponential backoff on 429 even though it was not observed in testing (defensive), but do not attempt to predict the budget from headers — there is no signal. Cap Flowboard's own concurrent dispatch at 3-4 jobs per key to keep wall-clock latency under 3 minutes per job.

## 7. Differences vs Google Flow (relevant for the abstraction layer)

| Aspect | Flow | Dreamina (Seedance) |
|---|---|---|
| Auth | Browser-session Bearer via extension | API key Bearer in header (`Authorization: Bearer ark-…`) |
| Captcha | Required per submit | None |
| First frame | media_id (Flow-internal) | Public HTTPS URL on each `image_url.url` (or base64 data: URL — untested) |
| Multi-ref | `IMAGE_INPUT_TYPE_REFERENCE` array — positional binding | **Not supported on seedance-1-5-pro.** First/last keyframes only via `role: "first_frame" / "last_frame"`. |
| Async pattern | Operation poll + `batchCheckAsyncVideoGenerationStatus` | Single `task_id`, single poll endpoint, status enum |
| Output | Signed `flow-content.google` URL OR inline base64 (low-priority) | Signed TOS URL only. File auto-deleted at 24 h. |
| Aspect ratios | `VIDEO_ASPECT_RATIO_LANDSCAPE` / `PORTRAIT` enums | Inline prompt flag `--rt W:H` (free-form-ish; verified: 16:9, 9:16, 1:1) |
| Duration | Provider-fixed | `duration` field (verified: 5, 8, 10 s) |
| Resolution | Provider-fixed | Inline prompt flag `--rs Np` (verified: 720p, 1080p) |
| Audio track | None | **Yes**, AAC stereo, on by default (`generate_audio: true`). No probe of the off-switch. |
| Per-request cost | Implicit from model key + tier (Flow charges credits) | `usage.completion_tokens` in response; ~21,700 tokens/sec @ 720p 1:1 |
| Error envelope | Flow-specific | Uniform `{"error": {"code", "message", "param", "type"}}` with HTTP status alignment (400/401/404) |

This table is the punch list for the `VideoProvider` Protocol in Phase 5.1.

## 8. Sample end-to-end curl session

```bash
KEY="ark-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX-XXXXX"
BASE="https://ark.ap-southeast.bytepluses.com/api/v3"
IMG="https://example.com/character.png"

# 1. Submit — returns {"id": "cgt-..."}
TID=$(curl -sS -X POST "$BASE/contents/generations/tasks" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "seedance-1-5-pro-251215",
    "duration": 5,
    "content": [
      {"type": "text", "text": "A girl turns her head and smiles --rt 16:9 --rs 1080p"},
      {"type": "image_url", "image_url": {"url": "'"$IMG"'"}}
    ]
  }' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")

# 2. Poll every 15s until succeeded — usually 90-220s total
until [ "$(curl -sS -H "Authorization: Bearer $KEY" \
  "$BASE/contents/generations/tasks/$TID" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")" = "succeeded" ]; do
  sleep 15
done

# 3. Fetch output URL and download immediately (file auto-deletes in 24h)
VURL=$(curl -sS -H "Authorization: Bearer $KEY" \
  "$BASE/contents/generations/tasks/$TID" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['content']['video_url'])")
curl -sS -o output.mp4 "$VURL"
```

## 9. Open questions still worth resolving before Phase 5 begins

- [ ] Base64 `data:` image input: does this model accept it? (Would remove the public-URL hosting dependency.)
- [ ] `generate_audio: false` override: is it a top-level body field? Audio adds bandwidth + may not always be wanted.
- [ ] Which model variant supports `task_type=r2v` (multi-reference)? Probe `seedance-1-5-lite` and any newer `r2v`-suffixed model.
- [ ] Maximum image dimensions / file size cap on `image_url`.
- [ ] Exact USD price per 1M tokens for `seedance-1-5-pro-251215` (BytePlus console).
- [ ] Real-world content-filter failure shape (provoke with a hard prompt in a separate test once policy allows, or capture in production once it happens organically).
- [ ] Confirm the `failed` and `cancelled` status payloads — see §3.
- [ ] Decide error vocabulary for the VideoProvider Protocol: unify codes (`content_filtered`, `auth`, `quota`, `bad_input`, `internal`) or keep provider-prefixed (`dreamina:InvalidParameter` / `flow:CONTENT_FILTER_TRIGGERED`).

## 10. Pointer to evidence

- Sanitized canonical samples: [docs/samples/](samples/)
  - `submit_minimal.json`, `submit_keyframe.json`, `submit_with_options.json`
  - `poll_running.json`, `poll_succeeded.json`
  - `error_auth.json`, `error_invalid_model.json`, `error_missing_param.json`, `error_invalid_role.json`, `error_multiref_unsupported.json`, `error_bad_image.json`, `error_task_not_found.json`
- Raw probe output (gitignored): `docs/samples/raw/`
