# Cinematic prompt writer

The Automation **viết prompt** button and production-run writer jobs use GPT via the existing Avis client. The default engine is `cinematic-v1`, derived from the director-style prompts approved for the ten 0925 clips. It works from each project's data; it does not insert the campus story, character names, style or props into other films.

## What the writer receives

- The full ordered shotlist, supplied timing, dialogue and speaker/delivery labels.
- Character profiles and approved wardrobe, environment, props and background groups.
- Actual reference images in the same positional order used for generation, including shared atlases and continuity frames. Images are loaded from the media cache or allowed media-storage URLs; a failed image read stops writing with an error.
- The previous clip's end state, scene production context and shot package when available.

For model inspection, images are resized to a maximum 1600-pixel edge and sent using Avis's native `imageBase64` format. Original reference IDs and SHA-256 hashes are saved in the result; model-input image bytes are not persisted. Generation continues using the original references. Up to 30 positional reference images are accepted, matching the current Seedance 2.5 adapter.

## Output and checks

The generated prompt follows this order: opening duration/ratio/style, **REFERENCE CONTROL**, **STYLE**, dramatic intention, opening state, ordered **SHOT** blocks, **AUDIO**, and **CONTINUITY / NEGATIVE CONSTRAINTS**. Each shot includes duration, framing, camera, physical action, exact dialogue with delivery labels when present, and an end state.

The model explicitly tracks clothing, crowd presence across cuts, prop scale and custody, occupied hands, contents, occlusion, entrances and exits. An empty environment sheet is not an instruction to empty the scene. A reference-sheet pose is not blocking. An off-camera object still exists in the scene. English dialogue stays English; dialogue is preserved in its supplied language rather than translated.

Compatible choices for previously unspecified hands, framing, or small visible continuity transitions are recorded separately as `staging_decisions`. They do not edit source evidence. Contradictory source facts still stop the writer; it cannot silently change a known holder, event, cut or line.

Code checks reference order, shot boundaries, duration, ratio, required sections, exact quoted dialogue and speaker/delivery labels. An independent GPT review checks every supplied requirement against the actual shot text and surrounding continuity context. It may request a bounded rewrite. This checks prompt quality against supplied data, not the resulting video or the original audio. It cannot guarantee the generator will render every instruction correctly.

One successful clip normally needs one writer call and one review call. Review requirements above 64 are split into chunks with concurrency four. The existing writer permits two ordinary attempts and at most one additional semantic repair; it does not loop indefinitely. Cinematic failures return an error instead of silently substituting a template.

## Integration and compatibility

- No database migration or provider credentials change is required.
- Engine name, staging decisions and inspected-reference metadata survive durable-job recovery and board reloads. The video node displays a `cinematic` badge and expandable staging notes.
- Existing approved prompt text is not bulk rewritten. Existing contract digests and signing rules remain in place. Use **viết prompt** to produce a new draft with this engine; normal source-change invalidation still applies.
- An explicit new writer run captures the board's existing `preserveSourceShots` timing choice, including fractional boundaries. Merely opening an older board does not add a timing field to its approved contract. Batch production already carries this choice.
- Provided cinematic drafts can be checked through `/video/verify-prompt` without rewriting their text. Optional `staging_decisions` are checked and included in signed coverage; they are not additions to source evidence.
- The approved example in `approved-example.txt` is a regression fixture and human reference only. Its story is deliberately excluded from model requests after a real test exposed cross-clip end-state contamination.

## Configuration

Existing environment configuration is retained:

```text
FLOWBOARD_PROMPT_WRITER=on
FLOWBOARD_PROMPT_WRITER_MODEL=gpt-6-luna
FLOWBOARD_PROMPT_REVIEW_MODEL=gpt-6-luna
FLOWBOARD_PROMPT_WRITER_FALLBACK=
```

The cinematic format is the shared production standard; `FLOWBOARD_PROMPT_ENGINE`
no longer selects another format. `/video/prompt` is a deprecated alias of
`/video/write`, including its review and reference checks. The service defaults to
cinematic writing as well. The explicit internal legacy branch is retained only
for regression tooling; no app endpoint exposes it. `FLOWBOARD_PROMPT_WRITER=off`
returns a clear error for new video prompts instead of producing a template.
All model calls use the existing Avis transport and credentials.

## Validation

Covered by `test_cinematic_prompt.py`, the existing writer/coverage/receipt suites, and frontend automation contract/runtime tests. A local live smoke run used the real 0925 clip 9 data and eight approved reference images through GPT-6 Luna on Avis: two shot blocks, one writer call, one independent review, approximately 35 seconds. This is one observed clip latency, not a forecast for every film or batch. No video generation was submitted by that test.
