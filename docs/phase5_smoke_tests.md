# Phase 5 Smoke Tests

Manual verification steps for the video provider integration. Run after
the unit test suite passes (`451+ tests`) to confirm end-to-end behavior
against real upstream services.

## Prerequisites

- BytePlus ARK API key in `~/.flowboard/secrets.json` under
  `apiKeys.dreamina` (see `docs/dreamina_api_contract.md` §1)
- R2 bucket + credentials configured (see `docs/r2_setup.md`)
- (Optional) Pricing rate set in `~/.flowboard/pricing.json`:
  ```json
  {"video": {"seedance-1-5-pro": {"usd_per_million_tokens": 0.0}}}
  ```
  Leave at `0.0` if the BytePlus rate isn't published yet — `cost_usd`
  will report `0.0` and the UI shows "Pricing not configured".

## 1. Backend boot

```bash
cd agent
.venv/bin/python -m flowboard
```

Expected: agent starts, no errors. Hit `GET /api/video/models`:

```bash
curl -sS http://localhost:8101/api/video/models | python3 -m json.tool
```

Expected output (shape):

```json
{
  "default_model_id": "flow-default",
  "models": [
    { "model_id": "flow-default", "provider": "flow", ... },
    { "model_id": "seedance-1-5-pro", "provider": "dreamina",
      "capabilities": { "supports_multi_ref": false, ... } },
    { "model_id": "seedance-2-0", "provider": "dreamina",
      "capabilities": { "supports_multi_ref": true, "max_refs": 4, ... } }
  ]
}
```

## 2. R2 hoisting

```bash
.venv/bin/python -c "
from pathlib import Path
from flowboard.services.storage import prepare_image_url
img = next(Path('storage/media').glob('*.png'))
print('Source:', img)
print('Hosted:', prepare_image_url(img))
"
```

Expected: a presigned `https://<account>.r2.cloudflarestorage.com/...`
URL. `curl -I` it — expect `HTTP/1.1 200 OK` and a content-type
matching the source extension.

## 3. Dreamina end-to-end (seedance-1-5-pro)

This is the only model the user has activated. Probes through the
worker exactly as a real VideoNode dispatch would.

```bash
# Pick any cached media_id to use as the first frame
MID=$(ls storage/media | head -1 | sed 's/\..*$//')

curl -sS -X POST http://localhost:8101/api/requests \
  -H 'Content-Type: application/json' \
  -d "{
    \"type\": \"gen_video\",
    \"params\": {
      \"model_id\": \"seedance-1-5-pro\",
      \"prompt\": \"A subtle camera pull-back, cinematic\",
      \"project_id\": \"<your-project-uuid>\",
      \"first_frame_url\": \"$MID\",
      \"duration_seconds\": 5,
      \"aspect_ratio\": \"16:9\",
      \"resolution\": \"720p\"
    }
  }"
```

Poll the returned request id until `status == "done"` (90-220 s
typical). Expected on the `result` field:

- `external_job_id`: starts with `cgt-`
- `provider`: `"dreamina"`
- `model_id`: `"seedance-1-5-pro"`
- `cost_tokens`: ~108,900 for a 5s 720p 1:1 job (scales with
  pixels × fps × duration)
- `cost_usd`: 0.0 (unless you set the rate)
- `media_ids[0]`: a 40-char hex string (the SHA1-derived synthetic id)
- Video file persisted to `storage/media/<that_id>.mp4`

Open the video locally:

```bash
open "storage/media/$(jq -r '.result.media_ids[0]' <(curl -sS http://localhost:8101/api/requests/<rid>)).mp4"
```

## 4. Capability-drop warning (i2v model receives multi-ref)

Same as above, but pass `reference_images`:

```bash
curl -sS -X POST http://localhost:8101/api/requests \
  -H 'Content-Type: application/json' \
  -d "{
    \"type\": \"gen_video\",
    \"params\": {
      \"model_id\": \"seedance-1-5-pro\",
      \"prompt\": \"Same prompt\",
      \"project_id\": \"<your-project-uuid>\",
      \"first_frame_url\": \"$MID\",
      \"reference_images\": [\"$MID\"],
      \"duration_seconds\": 5,
      \"aspect_ratio\": \"16:9\",
      \"resolution\": \"720p\"
    }
  }"
```

Poll. Expected: job still succeeds, but `result.warnings` contains:

```
"Dropped 1 reference images: Dreamina Seedance 1.5 Pro (i2v) is i2v-only. ..."
```

The submitted prompt body has NO `role: "reference_image"` block — the
provider dropped them before assembling the API call. Verify by
inspecting agent logs (search `dreamina:`).

## 5. Seedance 2.0 r2v (mocked — real activation deferred)

Seedance 2.0 isn't on your account yet. Backend tests
(`test_video_provider_dreamina.py::test_r2v_model_accepts_multi_ref`)
cover the multi-ref code path via mocked HTTPX. Re-run after BytePlus
activates the model:

1. Verify `~/.flowboard/secrets.json` `apiKeys.dreamina` has the same
   key (BytePlus uses one key per account, all models share it)
2. Update `registry.py` `seedance-2-0` entry's `upstream_model_id` if
   BytePlus published a different one than `dreamina-seedance-2-0-260128`
3. Re-run step 4 above with `"model_id": "seedance-2-0"` + a `reference_images`
   array of 1-4 media_ids
4. Expected: no warning, video uses the additional refs as character
   anchors. Compare against the 1.5-Pro output to confirm visual
   consistency improvement

## 6. Failure modes

### Auth failure

```bash
# Temporarily corrupt the key
mv ~/.flowboard/secrets.json ~/.flowboard/secrets.json.bak
echo '{"apiKeys": {"dreamina": "ark-bogus"}}' > ~/.flowboard/secrets.json
```

Submit. Expected: request fails with `error == "auth:..."` within a
few seconds. Restore the real secrets.

### Content filter

Pick a prompt likely to trigger ARK's safety filter (Phase 5 contract
§3 notes the filter is mild; you may need to try several). The job
will reach the poll phase, then transition to `failed`. Expected:
`result.error == "content_filtered"` and `result.error_message` set.

### Timeout (local exhaustion)

In a Python REPL:

```python
from flowboard.services.video import dreamina
dreamina.DREAMINA_POLL_MAX_CYCLES = 1  # cycles
dreamina.DREAMINA_POLL_INTERVAL_S = 1  # second
```

Submit a real job. After ~2 s the worker will give up. Expected:
`error_message: "local poll exhausted after 1 cycles"`. The upstream
task is NOT cancelled — it will still finish and the bytes will be
unreachable. (Production cycles are 30 / 15s = 7.5 min ceiling, well
beyond typical jobs.)

### R2 misconfigured

Wipe the `r2` block from `~/.flowboard/secrets.json` momentarily and
submit a Dreamina job with a Flowboard media_id as `first_frame_url`.
Expected: 500 / 400 surfacing `"object storage is not configured —
see docs/r2_setup.md"`.

## 7. Flow regression

Verify Flow still works identically. Repeat your normal Flow flow
end-to-end — generate an image, then a video from it. Expected:
behavior matches pre-Phase 5 (positional `media_ids`, `slot_errors`,
`partial_error` shape preserved on `result`). Specific assertions to
check on `GET /api/requests/<rid>`:

- `result.media_ids` (array) — present
- `result.operation_names` (array) — present
- `result.partial_error` — present when any op failed, absent otherwise

If any of these are missing on the Flow path, the dispatcher's result
translation is broken — file a regression, revert before shipping.
