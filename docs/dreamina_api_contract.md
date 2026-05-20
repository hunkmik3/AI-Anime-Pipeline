# Dreamina (Seedance / Volcengine) API Contract

> **Status**: skeleton — needs to be filled in by manual curl testing before Phase 5 begins.
>
> This document captures the *external* contract Flowboard's `DreaminaVideoProvider` will speak. Phase 5 implements `agent/flowboard/services/video/dreamina.py` against this spec, so any vagueness here turns into rework there.
>
> Source for filling this in: the user's Dreamina/Volcengine account, the official API docs, and curl/httpie experiments. Claude cannot access these directly — please fill the `<TBD>` sections yourself.

## 0. Pre-requisites

- [ ] Dreamina / Volcengine account with API access enabled
- [ ] API credentials issued (key + secret? bearer token? OAuth?)
- [ ] Sample working curl that submits a video gen and one that polls a job

## 1. Authentication

| Field | Value |
|---|---|
| Auth scheme | `<TBD: bearer token | HMAC signature | API key in header | …>` |
| Required headers | `<TBD>` |
| Credential rotation | `<TBD: never | every N hours | on-demand>` |
| Where to store on Flowboard side | `~/.flowboard/secrets.json` under `apiKeys.dreamina` (same pattern as the LLM providers — see `agent/flowboard/services/llm/secrets.py`) |

### Sample auth header

```
<TBD: paste from working curl>
```

## 2. Submit endpoint (start a video generation)

| Field | Value |
|---|---|
| Method | `<TBD: POST>` |
| URL | `<TBD: https://...>` |
| Required body fields | `<TBD: prompt, first_frame_url|first_frame_b64, model, …>` |
| Optional body fields | `<TBD: last_frame, duration_seconds, aspect_ratio, seed, …>` |
| Response shape | `<TBD: { task_id: string, status: string } or similar>` |
| Failure modes | `<TBD: HTTP status codes + error envelope>` |

### Sample request body (working)

```json
<TBD>
```

### Sample response

```json
<TBD>
```

### First-frame / last-frame image handling

The Flowboard frontend gives us a `media_id` that resolves to a local file in `storage/media/{uuid}.{ext}`. Dreamina's API needs the image in one of these forms:

- [ ] URL (must be publicly reachable) — Flowboard would need to upload it somewhere first
- [ ] Base64 inline in the request body — preferred, no upload step
- [ ] Pre-uploaded asset ID inside Dreamina's own asset store — would need a separate upload call

Pick which one is supported and document the size cap.

| Image format | Supported? | Max size (px / bytes) |
|---|---|---|
| URL | `<TBD>` | `<TBD>` |
| base64 | `<TBD>` | `<TBD>` |
| Asset ID upload | `<TBD>` | `<TBD>` |

### Multi-reference support (for character/style anchor)

| Question | Answer |
|---|---|
| Can a single submit attach N reference images? | `<TBD: yes/no, how many>` |
| Does ordering matter (first ref = identity anchor)? | `<TBD>` |
| Are references typed (identity vs style)? | `<TBD>` |

## 3. Poll endpoint (check progress)

| Field | Value |
|---|---|
| Method | `<TBD: GET>` |
| URL | `<TBD>` |
| Auth | `<TBD: same headers as submit>` |
| Typical poll interval | `<TBD: how often does Dreamina say to poll, vs how often it actually changes>` |
| Total generation duration | `<TBD: 2 min / 5 min / 10 min>` |
| Status enum | `<TBD: pending | running | success | failed | content_filtered | …>` |

### Sample poll response (still running)

```json
<TBD>
```

### Sample poll response (success)

```json
<TBD>
```

### Sample poll response (failure / content filter)

```json
<TBD>
```

## 4. Output retrieval

| Question | Answer |
|---|---|
| Does the success response inline the video bytes (base64) or a URL? | `<TBD>` |
| If URL: is it signed? What's the expiry? | `<TBD>` |
| If URL: domain it resolves to (for the Flowboard URL allowlist)? | `<TBD>` |
| Video container/codec | `<TBD: mp4 h.264 / webm vp9 / …>` |
| Duration of the clip | `<TBD: fixed 4s? configurable 4-8s?>` |
| Resolution options | `<TBD>` |

## 5. Cost / billing

| Question | Answer |
|---|---|
| Cost per submit (USD, or token units) | `<TBD>` |
| Where in the response is the cost reported? | `<TBD: probably not — likely need to compute from model+duration>` |
| Free tier / monthly quota | `<TBD>` |

Cost must be writable to `Request.result.cost_usd` per Phase 5.5.

## 6. Rate limits

| Limit type | Value |
|---|---|
| Requests per minute | `<TBD>` |
| Concurrent jobs | `<TBD>` |
| Per-day cap | `<TBD>` |

## 7. Differences vs Google Flow (relevant for the abstraction layer)

| Aspect | Flow | Dreamina |
|---|---|---|
| Auth | Browser-session Bearer via extension | API key (likely) |
| Captcha | Required per submit | `<TBD: probably none>` |
| First frame | media_id (Flow-internal) | URL or base64 (TBD) |
| Multi-ref | `IMAGE_INPUT_TYPE_REFERENCE` array — positional binding | `<TBD>` |
| Async pattern | Operation poll + `batchCheckAsyncVideoGenerationStatus` | `<TBD: likely simpler — single task_id poll>` |
| Output | Signed `flow-content.google` URL OR inline base64 (low-priority) | `<TBD>` |
| Aspect ratios | `VIDEO_ASPECT_RATIO_LANDSCAPE` / `PORTRAIT` enums | `<TBD: free-form WxH? enum?>` |
| Per-request cost | Implicit from model key + tier (Flow charges credits) | `<TBD>` |

This table is the punch list for the `VideoProvider` Protocol in Phase 5.1.

## 8. Sample end-to-end curl session

Paste a captured terminal session that submits, polls, and retrieves a video. This is the canonical "if I copy these three commands they work" reference for Phase 5.

```bash
# 1. Submit
<TBD>

# 2. Poll (repeat until done)
<TBD>

# 3. Fetch output
<TBD>
```

## 9. Open questions for the user before Phase 5 begins

- [ ] Which Dreamina region/endpoint will Flowboard target? (cn-north / ap-singapore / …)
- [ ] Will we standardise on a single model (e.g. `seedance-v2`) or expose model choice in the VideoNode settings?
- [ ] How should Flowboard surface Dreamina-specific failure codes vs Flow's content filter codes — unify the error vocabulary, or keep provider-prefixed (`dreamina:<code>` / `flow:<code>`)?
- [ ] If Dreamina requires public-reachable URLs for input images: do we use a temporary signed URL via Flowboard's existing media route (would need to expose it publicly), or pre-upload to Dreamina's asset store?
