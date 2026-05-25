# Phase 6.7 — Seedance 2.0 verified request payloads

> **Purpose**: capture the exact request shapes from the 4 live Seedance 2.0
> probes (2026-05-25) so the `DreaminaVideoProvider` can be diffed against
> them. These are the payloads behind the "70-80% quality" baseline the user
> is comparing against.
>
> **Evidence provenance**: the probes used **real character references and
> hand-written prompts that were NOT committed** (§11.8 of
> `docs/dreamina_api_contract.md`: "No canonical samples committed"). The
> payload *bodies* below are **reconstructed from contract §11** (the
> authoritative, evidence-backed record) with keys/URLs/prompt-bodies
> scrubbed. The **results, token costs, and structural facts are verified**
> (they come straight from §11.6 / §11.8 and the downloaded outputs
> `storage/media/seedance2-test{1,3,4}-*.mp4`).
>
> What is NOT reconstructable: the literal prompt text of each probe (the
> raw curl bodies were not saved). Structure and section names are known;
> exact wording is not.

Common to all four:

| Field | Value |
|---|---|
| Method / URL | `POST https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks` |
| Headers | `Authorization: Bearer ark-<scrubbed>`, `Content-Type: application/json` |
| Model | `dreamina-seedance-2-0-260128` |
| Poll | `GET …/contents/generations/tasks/{id}` → `status` enum, `usage.completion_tokens` |
| **Inline `--rt/--rs` flags** | **ABSENT in all four probes.** Aspect/resolution were left to model default. §11.7: "r2v aspect/resolution handling on 2.0 is **untested**." |

---

## Test 1 — Multi-ref r2v (3 × `reference_image`)

**Request body** (reconstructed shape; 3 refs per §11.8 "role=reference_image ×3"):
```json
{
  "model": "dreamina-seedance-2-0-260128",
  "duration": 5,
  "content": [
    {"type": "text", "text": "<prompt; references described as @image1 / @image2 / @image3 in text>"},
    {"type": "image_url", "image_url": {"url": "<ref1 https/R2 url>"}, "role": "reference_image"},
    {"type": "image_url", "image_url": {"url": "<ref2 https/R2 url>"}, "role": "reference_image"},
    {"type": "image_url", "image_url": {"url": "<ref3 https/R2 url>"}, "role": "reference_image"}
  ]
}
```
- **Response**: `{"id":"cgt-…"}` → poll → `succeeded`.
- **Token cost**: ≈ **108,900** (5 s). *(verified, §11.6/§11.8)*
- **Output**: `storage/media/seedance2-test1-multiref.mp4` (1.8 MB).
- **Observed behavior**: r2v accepts N `reference_image` blocks; **no
  `first_frame` present**. `@imageN` binds to the Nth `reference_image`
  positionally (§11.2). No `--rt/--rs`.

## Test 2 — Audio reference (shape verified only)

**Request body**:
```json
{
  "model": "dreamina-seedance-2-0-260128",
  "duration": 5,
  "content": [
    {"type": "text", "text": "<prompt>"},
    {"type": "image_url", "image_url": {"url": "<ref https/R2 url>"}, "role": "reference_image"},
    {"type": "audio_url", "audio_url": {"url": "<voice https/R2 url>"}, "role": "reference_audio"}
  ]
}
```
- **Response**: schema validates; with a **bogus** audio URL the task fails at
  fetch: `content[2].audio_url … resource download failed` (proving the field
  is supported, not the value). *(verified, §11.3/§11.8)*
- **Token cost**: n/a (never reached generation).
- **Output**: none (no real voice sample; full audio-driven gen NOT run e2e).
- **Constraints (verified)**: audio = "reference media mode" →
  - image **must** be `role:"reference_image"` (role-less image + audio →
    `first/last frame content cannot be mixed with reference media content`);
  - `reference_image` + audio without audio role →
    `reference media mode requires audio role to be reference_audio`;
  - **no `first_frame` allowed** in this mode.

## Test 3 — Multi-shot, 8 s

**Request body** (reconstructed; §11.4 + §11.8 "1 image, 2-shot prompt"):
```json
{
  "model": "dreamina-seedance-2-0-260128",
  "duration": 8,
  "content": [
    {"type": "text", "text": "[SHOT 1] (0-4s) … [SHOT 2] (4-8s) … Hard cut at 4 seconds."},
    {"type": "image_url", "image_url": {"url": "<ref https/R2 url>"}}
  ]
}
```
- **Response**: `succeeded`.
- **Token cost**: ≈ **173,700** (8 s — cost tracks duration only). *(verified)*
- **Output**: `storage/media/seedance2-test3-multishot.mp4` (2.5 MB).
- **Observed behavior**: multi-shot is purely a **prompt-text** convention
  (time-coded `[SHOT n] (a-bs)` beats + "Hard cut"); the API has no shot-cut
  field. Whether the cut is genuine vs. blended is a visual-QA item.
- **Note**: the probe log records "1 image" but does **not** pin the image's
  `role`. The probe's focus was `duration:8` + the 2-shot text. Treat the
  role as unverified for this test.

## Test 4 — Long structured prompt (4,483 chars) + 2 × `reference_image`

**Request body** (reconstructed; §11.5):
```json
{
  "model": "dreamina-seedance-2-0-260128",
  "duration": 8,
  "content": [
    {"type": "text", "text": "ART STYLE: …\nANIMATION CADENCE: …\nREFERENCE MAPPING: @image1 = <char A> …, @image2 = <char B> …\nSTORYBOARD: …\nAUDIO: …\nCONSISTENCY: …\nNEGATIVE: …"},
    {"type": "image_url", "image_url": {"url": "<ref1 https/R2 url>"}, "role": "reference_image"},
    {"type": "image_url", "image_url": {"url": "<ref2 https/R2 url>"}, "role": "reference_image"}
  ]
}
```
- **Response**: `succeeded`, no truncation / "prompt too long". *(verified)*
- **Token cost**: ≈ **173,700** (8 s — identical to Test 3; cost is
  duration-only, independent of prompt length / ref count). *(verified)*
- **Output**: `storage/media/seedance2-test4-ep1.mp4` (3.0 MB).
- **Observed behavior**: a 7-section structured prompt with an explicit
  **REFERENCE MAPPING** section (semantic `@imageN = …` descriptions) is
  accepted. ~5,000-char prompts are feasible. This is the **richest** of the
  four prompts and the most likely source of the "70-80%" quality bar.

---

## Cross-cutting facts (verified)

1. **No `--rt/--rs` in any probe.** Aspect/resolution were never set on 2.0.
2. **`@imageN` semantics live in the prompt text**, with *descriptions*
   ("@image1 = character left …"), not as bare tags. Role field is only ever
   `reference_image` / `reference_audio` / `first_frame` / `last_frame`.
3. **No `first_frame` in r2v or r2v+audio.**
4. **`duration` is a top-level int** (5 / 8), never an inline `--dur` flag.
5. Cost is **duration-only**: 5 s ≈ 108,900; 8 s ≈ 173,700.
