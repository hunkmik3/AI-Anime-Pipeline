# Source shotlist — one-pass mode

`analysis_mode=one_pass` is the default source upload mode, “Đọc một lượt — lập shotlist”.
With **Tự chạy đến phim hoàn chỉnh** enabled, upload the source and select a film
style: the durable worker continues through source analysis, production catalog,
image sheets, cinematic video prompts, clip generation and movie assembly. The
browser does not need to remain open. Turn automatic production off to stop at
the shotlist. Existing standard/fast projects keep their checkpoints and behavior;
approved prompts and existing films are not bulk rewritten.

The source path runs:

1. Existing cut/keyframe measurement, with the normal cache.
2. Local Whisper medium ASR, in a separate process. Probe language throughout the
   audio every five seconds, refine detected transitions with two-second windows,
   then decode language spans in bounded, overlapping windows. No manual language
   switch timestamp or title-specific rule is required. A supplied language remains
   an explicit override. Silent windows are skipped using VAD. This is ASR, not a
   guarantee of exact wording; detection and confidence findings are retained.
3. One visual observation per four shots, three requests concurrently, using
   `FLOWBOARD_ONE_PASS_MODEL` (default `claude-opus-5-5`) through Avis. Source frames
   are supplied, not generated reference images. All caption changes are stored
   separately, with per-shot frame IDs. Crop, elevation and composition are separate.
4. Compile original-language audio sentences into a timestamped dialogue track.
   Sentences can cross cuts. Spoken repetitions remain; translated captions never
   replace audio. Missing ASR stays missing and can produce a localized finding.
5. Existing story/entity extraction, each with one application/transport attempt
   and a 180-second deadline. This is summarization, not an independent review.
6. Local structural validation; return the shotlist, entities and uncertainties.

No tier escalation, independent source-inventory verifier, protocol review, identity
repair agent or whole-film source-refinement loop runs in this mode.

## Automatic production after the shotlist

1. Re-decode only audio windows flagged by ASR, once, locally. Preserve word ownership
   at boundaries. Unresolved audio stays blocked; never invent or omit dialogue.
2. Prepare a chronological catalog with GPT via Avis (default `gpt-6-luna`, override
   `FLOWBOARD_ONE_PASS_CATALOG_MODEL`). Batches default to 12 shots, configurable
   with `FLOWBOARD_ONE_PASS_CATALOG_BATCH`, maximum 16. Supply observations, exact
   dialogue, actual shot frames, existing identities and previous continuity state.
   One person keeps one ID; costumes are separate states. Groups, environments,
   prop ownership, arrivals and departures are carried into shot membership.
3. Validate IDs, frame citations, wardrobe bindings, speaker coverage and prop
   transfers in code. Only concrete failed checks receive one local catalog repair.
   Do not manufacture a visual-verification result or accept unresolved findings.
   Explicit environment bindings select location references without inventing
   an on-screen/off-screen visibility claim; the observed camera/crop determines
   how much of the setting appears.
4. Compile the board from source shots in code: preserve cuts, camera observations,
   exact source-language lines and speakers. Record evidence-backed corrections
   separately. Choose clip boundaries outside spoken sentences (maximum 30 seconds);
   costume changes also start a new clip so each character has one wardrobe
   reference per clip. A sentence crossing a required costume cut, or a take with
   no safe boundary within 30 seconds, currently stops with a specific error
   instead of silently cutting or dropping speech.
5. Generate approved style-specific character/environment/prop sheets. Reuse a
   character identity sheet for its only costume; generate extra states when needed.
   Source states constrain design, but remain separate from reference variants.
   A prop sheet's incidental holder is not a replacement character/wardrobe anchor.
   Style notes come from the selected preset (or a bounded custom STYLE section),
   never from the following layout/profile text.
6. Use the approved GPT/Avis cinematic writer and actual generated references, then
   the existing per-clip prompt checks. Camera, framing, ordered action beats and dialogue are serialized
   from locked inputs. Retain style-specific motion standards and scene raccord.
   Source cut times and header formatting are serialized in code, without asking
   the model to recalculate them. Visual-frame observations of mouth movement do
   not establish audible words; ASR utterance windows are approximate alignment,
   not phoneme-accurate cut boundaries. The reviewer retains this modality scope
   while still checking exact dialogue, speakers and genuinely conflicting facts.
   On an explicit retry, an existing paid draft with the same complete input
   digest is reused, including its staging decisions. It still goes through all
   prompt checks and independent semantic review before receiving a receipt.
   Image-sheet creation prompts and legacy noncharacter designer `states` are
   excluded from the model's video context; their full provenance remains stored
   and hashed. Shot observations, character costumes and material geometry remain
   intact. A sheet designer's proposal cannot replace an observed shot state.
7. Generate clips with bounded concurrency and feed completed clips to the existing
   assembler. Failed/ambiguous paid jobs retain their checkpoints for reconciliation.

The source report is `method=one_pass_production`, `status=observed`,
`structural_checks_passed=true`, with `prepared_shots` and `independent_review=false`.
It is a distinct production-readiness contract, **not** `source_frames/verified`.
The server checks inventory/shot/asset digests, exact shot locks and reference coverage
before production; editing locked camera/dialogue/wardrobe fields invalidates it.
The old independently verified path still uses its original contract.

Raw cuts and IDs remain unchanged in the app implementation. The earlier benchmark's
two suggested cut merges were evidence-backed experiment results, not a blanket rule
to merge all short shots. This version flags suspicious short cuts and does not
automatically apply those experiment-specific edits to another film.

## Checkpoints and timing

Visual batch fingerprints include model, prompt version/text, source timing and image
bytes. Successful siblings survive failure. In-flight/failed/unknown journals are
not automatically resubmitted; investigate the journal before intentionally retrying.
The new ASR cache is separate from legacy `transcript.json` and bound to source bytes,
language override, model and version. Machine cache timings are identified as cached
and excluded from new-mode execution timing. Model latency is not cold end-to-end time.

## Automatic production safeguards added 2026-10-07

- Upload accepts an optional `casting_request`. One checkpointed GPT/Avis call maps
  explicitly requested ethnicity/skin tone/hair colour/eye colour to existing source
  IDs. Ambiguous role mappings stop before material generation. Target profiles and
  owned per-shot appearance descriptions receive a separate casting receipt; source
  observations, age, build, wardrobe, camera, spoken words and speaker labels remain
  unchanged. Empty requests retain the original cast.
- Offscreen carry retains custody and contents, but keeps old screen position and
  visibility prose as last-visible metadata rather than current composition claims.
- Per-plate `referenceScope` reaches the shot package, prompt writer and reviewer.
  A head-only sheet cannot be described as evidence of clothing, feet or body pose.
- A typed, terminal empty catalog response gets one bounded retry, recorded in its
  journal. Timeouts, unknown outcomes, content refusals and malformed nonempty
  replies do not use this exception. Resume cannot reset the two-attempt budget.
- A completed clip can survive a scene replan caused by later edits only when its
  own source/target facts, incoming state, actual reference bindings, material
  versions and generation settings still match. Its original paid contract is kept;
  this does not weaken validation for a new submission. Unknown jobs still block.
- New automatic films default to `timing_policy=full_take`: retain all generated
  audio/video instead of assuming extra provider seconds are silent. Output can be
  longer than the source. Normalize resolution and final FPS; record measured and
  editorial durations separately. Closing-state references use the actual take end
  at a clip boundary. `source_duration` remains available for deliberate exact cuts.
- Every newly written cinematic draft receives `NO BACKGROUND MUSIC. NO BGM.
  NO SCORE.` inside AUDIO. Previously approved prompts are not bulk rewritten;
  this instruction does not certify that generated audio is music-free.

Validation uses mocked provider orchestration plus real FFmpeg media tests, including
the five existing Unexpected Pregnancy takes. No new paid generation is implied by
these checks; a fresh unseen film still depends on ASR/model accuracy and provider
availability.

## Validation on Haven, 2026-10-06

Artifacts: `pipeline-tests/haven-one-pass-app-v2` in the owner's experiment workspace.
The live service-path trial used the first 63 source seconds (34 shots), cached
cut/keyframes, fresh Opus observations and a just-measured multilingual ASR result.
Audio: about 60 seconds, no manual transition marker or recovery call. Vision/story:
about 152 seconds, nine Opus requests plus two GPT story requests. ASR cache was reused
in the pipeline run and is reported separately; these are not two fresh ASR runs.

Recovered breakup, disbelief, company-offer and travel-together lines; caption events
at source shots 10/12/13 retained all eight sampled caption changes. This does not
prove every word correct. Known remaining errors include a phrase around 40 seconds,
Vietnamese intro words, and source shot 16 elevation/crop. The analysis benchmark itself generated no video; later production trials are recorded separately.
