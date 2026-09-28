# Source inventory and prompt agents

This pipeline applies to each uploaded film independently. Characters, groups,
locations, props, identities and shot membership come from that film. There is no
default cast, setting, group size or prop list. The house prompt examples define
writing format; their story facts are not production requirements.

```mermaid
flowchart LR
    V[Source video] --> S[Measured shots and source descriptions]
    S --> I[Agent 1: inventory and source frame tools]
    I --> R[Independent source verification]
    R -->|needs review| I
    R --> L[Separate graphics from physical assets]
    L --> D[Compare duplicate identities visually]
    D --> Q[Correct shots and independently verify]
    Q --> A[Adaptation and production assets]
    A --> B[Board: shots, references and source evidence]
    B --> W[Agent 2: prompt writer]
    W --> C[Per-shot coverage and independent semantic review]
    C -->|prompt defects, bounded retry| W
    C --> G[Check freshness, then generate]
    G --> H[User inspects output video]
```

## Use in the existing application

1. New video analyses automatically run inventory extraction and source
   verification. Existing analyses can use **Đối chiếu video gốc** in the source
   review tab (`POST /api/automation/videos/{id}/verify`). This preserves the
   adaptation and generated plates; re-import the updated board afterwards.
2. Review the separate source report. `verified` means the supplied source
   frames passed the visual checks, or a person explicitly accepted the retained
   findings. It does not certify the audio or every intervening video frame.
   `needs_review` and `unverified` retain unresolved visual findings and block
   writing a source-verified generation prompt. The UI separates those findings
   from informational limits of the verification method.
3. Build the cast, write design briefs, and generate required references.
   Recurring groups and props have their own assets. Generate dependencies first:
   for example, a depicted person's identity sheet precedes an image containing
   that person. The same mechanism covers group members and container contents.
4. Import the board. It keeps evidence, prop state/holder, background presence,
   references and the separate source report. Imported plates remain usable.
5. Write the clip prompt. Agent 2 preserves the existing six-section format,
   checks every required fact inside its own shot, and independently reviews
   meaning. Changes to shots, assets, references, style or previous state make a
   stored prompt stale. Gen requires a current server-issued coverage receipt.

Strict source contracts currently use the image reference generation mode.
The provider's keyframe and previous-video modes drop these image references,
so they are refused for this contract rather than being labelled covered.
Existing freeform/script boards retain their prior behavior.

## Data contract

`analysis.scene_inventory` contains `schema_version`, `assets`, `scenes`, and
source `shots`. Asset kinds are `character`, `background_group`, `prop`, and
`environment`; IDs are scoped to that source video. Character roles and group
membership are data, not genre-specific logic. Empty categories are valid.

Each shot records `asset_presence`: `visible`, `partial`, `occluded`, `offscreen`
or `uncertain`, with position, state, optional holder/hand, contents and evidence
IDs. Scene membership is the union across the scene's timeline. It never forces
someone who arrives later into earlier shots. Offscreen is not an inferred exit.

`analysis.source_verification` records actual supplied frame IDs/timestamps,
reviewed and unresolved shots, findings, tool trace, usage and provenance hashes.
The host creates evidence IDs and validates frame/crop requests. Generated model
confidence is not sufficient for a verified status.

`source_verification.scope_notes` separately records method limitations such as
`audio_not_checked` and `continuous_motion_not_checked`. These host-written notes
are informational; they do not change shot verdicts or require human acceptance.
The verifier receives visual source descriptions and visible captions, without
ASR dialogue to judge as unheard audio. Every finding actually returned by the
verifier remains blocking: the host does not discard a warning because its text
mentions audio or motion. A concrete visual contradiction, missing evidence, or
unresolved identity remains a finding. Human acceptance keeps findings, scope
notes, and the original machine verdict; it does not repair descriptions or
verify speech.

Cast entries join to observed identities by `source_asset_id`; target names and
creative designs stay separate from source facts. Board shots retain ordered
`source_appearances`, so merging short shots preserves prop transfers and
visibility changes. JSON export includes inventory and source verification.

Agent 2 returns `coverage`, `contract_digest`, and a signed `coverage_token`.
Coverage quotes must exist in the specified shot, and semantic review checks
identity, visibility, relationships and state. Generation validates these plus
the exact prompt, reference URLs/order, media IDs, duration and aspect ratio.
Strict failures return 422 and never silently fall back to an unchecked template.

## Configuration and bounded execution

Both agents use **GPT-6 Luna through the existing Avis provider** by default,
with `AVIS_API_KEY` and the existing `AVIS_BASE_URL` configuration. Inventory
extraction and source verification are separate calls with separate instructions;
prompt writing and semantic review also remain separate calls. Using the same
model does not make either review independent of that model's shared limitations.
No new SDK or database migration is needed for these additive JSON fields.

| Purpose | Environment variable | Default |
|---|---|---|
| Source inventory | `FLOWBOARD_INVENTORY_MODEL` | `gpt-6-luna` |
| Source concurrency | `FLOWBOARD_SOURCE_CONCURRENCY` | `4`; configurable 1–64; fast profile uses 48 |
| Parallel observation | `FLOWBOARD_SOURCE_OBSERVATION_CONCURRENCY` | `1`; fast profile uses 32 |
| Source verifier | `FLOWBOARD_SOURCE_VERIFY_MODEL` | Configured inventory model, in a separate call/context |
| Character/image and clip prompt writer | `FLOWBOARD_PROMPT_WRITER_MODEL` | `gpt-6-luna` |
| Prompt fallback model | `FLOWBOARD_PROMPT_WRITER_FALLBACK` | Empty: cross-model fallback disabled |
| Prompt semantic reviewer | `FLOWBOARD_PROMPT_REVIEW_MODEL` | Configured writer model, in a separate call/context |
| Enable prompt writer | `FLOWBOARD_PROMPT_WRITER` | `on` |

The `gpt-6-luna` alias was confirmed in the Avis model catalog on 2026-09-25 as
active with text/image input and text output. These are Avis gateway aliases,
not a claim about public model availability elsewhere. Dedicated environment
overrides retain precedence; restart the backend after changing them. The older
`FLOWBOARD_VISION_TIER1`/`FLOWBOARD_VISION_TIER2` settings no longer select the
inventory agents, and the other shot-analysis/adaptation stages are unchanged.
The writer cannot switch to Claude or another model unless an administrator
explicitly sets `FLOWBOARD_PROMPT_WRITER_FALLBACK`. Existing legacy template
fallback behavior is separate and remains labelled as a template, not Luna.
Model calls are mocked in implementation tests.

Agent 1 works in six-shot batches, makes at most five logical model calls per
batch, permits two extra-frame requests per inspection round and 32 extra frames
per film. Calls may have bounded transport retries. High-resolution/crop evidence
uses an FFmpeg subprocess; it does not load video decoding libraries into the
server. Cache fingerprints include source and frame contents, shot data, models,
prompts and limits, so one film cannot reuse another film's observations.

The visual verifier separates defects in the inventory from defects in the
original shot description (`source_description_findings`). Both block the
affected shots. A description-only defect is reported after verification instead
of spending another inventory repair cycle: that extractor cannot overwrite the
original description. Mixed or inventory defects still use the bounded repair
pass, and additional visual evidence can still be requested.

Source observation registers identities in source order. Verification and bounded
repair overlap across batches, sharing one concurrency limit with observation.
Workers receive immutable identity catalogs; pending prop state and hand/holder
claims are never used as established facts in later batches. Identity changes,
retracted observations and late identities retain explicit review findings.

The source journal checkpoints each completed model stage, including unresolved
verdicts. Interrupted runs reuse compatible observations and checks; transient
failed stages retry. Cache reuse never means human acceptance. Story sequences
and entities are also cached by source descriptions, models and instructions, so
restarting source verification does not regenerate its upstream identity inputs.
Old journal policies are retained as legacy files and rechecked under the new
policy. The report records configured and observed model concurrency.

Agent 2 allows at most two writer attempts, each with an independent review.
Source uncertainties return upstream instead of being invented by the writer.
The server validates and hashes the full input, then sends only the current
clip's asset/dependency closure and evidence to the model. Camera, lens, lighting,
effects, dialogue and action requirements remain intact when supplied.

## Limits and verification

Source verification is based on sampled frames and targeted reinspection. It
does not guarantee exhaustive frame-by-frame perception, nor independently
verify audio; the existing ASR/dialogue pipeline remains responsible for speech.
It never inspects generated output videos. Model identity matching and semantic
judgment remain fallible; findings are retained for review.

The UI labels the result as visual verification and shows audio as independently
unchecked, including for older reports without `scope_notes`. A general inability
to certify continuous movement between sampled frames is a scope limitation;
contradictions in visible poses or object states still require source review.

Backend tests cover source evidence validation, budgets, failed/missing frames,
cross-film caches, overlapping groups, prop state transitions in merged shots,
reference dependencies, source provenance, prompt coverage and stale receipts.
Frontend store tests cover imported plates, missing-reference metadata,
cross-film resets, stale prompts and receipt transport. Run backend pytest in
one process: the repository's shared test database fixtures reset tables.


## Fast observation profile

For an Avis account that supports the concurrent load, set
`FLOWBOARD_SOURCE_CONCURRENCY=48` and
`FLOWBOARD_SOURCE_OBSERVATION_CONCURRENCY=32`, then restart the backend. These are
per-analysis limits, not a claim that the provider guarantees any throughput.
No quota-based delay is imposed. Transport retries remain bounded.

A compatible serial checkpoint is replayed first. Remaining image-heavy batches
are observed concurrently against one frozen identity seed. New asset and scene
IDs are scoped by batch to prevent accidental collisions. Ordered waves of six
batches visually reconcile candidate identities with canonical source anchors;
independent per-batch verification follows concurrently. Ambiguous matches stay
blocking, and a match cannot collapse two identities that co-occur in a shot.
All holders, contents, group members and dependent identities are remapped.

The journal retains speculative observations, identity mappings, exact context
fingerprints and completed checks. A resume reuses each compatible stage; mapping
uncertainty never disappears merely because a local verifier returns a clean
verdict. Extra source-frame budgets and source-description findings still apply.
New scene IDs remain scoped until a known source scene can be reused.

A strict parser can correct exactly one mismatched closing delimiter outside JSON
strings when the one-character substitution produces a complete valid object.
It preserves the raw response and correction provenance. Truncation and other
malformed data still require a bounded retry, and all evidence/structure checks
run after a syntax correction. Previously saved malformed responses are retried
locally before another model call is purchased.

## Recovery of identity checks

A partial identity response is validated candidate by candidate. Valid decisions
and explicit uncertainty are preserved; a bounded completion call requests only
missing rows. Invalid citations, incompatible kinds, co-occurrence and unresolved
dependencies still block their affected identities. Earlier canonical corrections
can invalidate downstream matching and QA contexts; observations stay reusable.

The host records first publication in the ordered canonical catalog separately
from the frozen dispatch seed. On resume, retraction findings are recomputed from
that publication provenance, with the previous derived result retained in the
journal. Raw model observations and genuine source findings are unchanged.

Repair-discovered identities receive a final visual reconciliation against the
completed catalog. New or unresolved identities also receive concurrent checks
for later shots where their presence was not recorded. Every shot/identity pair
must yield cited sampled-frame absence or a scoped blocking finding. Missing
images, malformed replies and provider failures remain blocking at those pairs.
This replaces blanket stale-context warnings with specific coverage findings; it
does not insert a missing character or grant human acceptance. Original visual
description defects and ambiguous identity findings remain reviewable. These
additional stages have separate journal entries and usage counts, share the same
provider concurrency bound, and reuse compatible saved responses.

## Source refinement and visual layers

`POST /api/automation/videos/{id}/refine` (UI: **Sửa & đối chiếu**) corrects an
existing source draft without repeating cuts, ASR or initial visual descriptions.
New analyses run this stage automatically; `FLOWBOARD_SOURCE_REFINEMENT=off`
disables the automatic stage, while the explicit endpoint remains available.

Two independent source-image decisions separate composited screen graphics from
physical people, places and props. Physical signs, books, photographs and printed
labels remain physical. `scene_inventory.screen_graphics` retains original
definitions and each shot's `screen_graphics` retains observed occurrences. No
film title, character name, regex or confidence score authorizes a migration.
Cross-layer physical dependencies remain reviewable.

Potential duplicate identities are proposed from the catalog, then require two
independent visual matches with their own image anchors. Wardrobe, X-ray effects,
pose and object state are not new identities. Different kinds, co-occurring
entities, conflicting group/dependency relationships and contrary pair reviews
cannot merge. Canonical IDs and original alias records are retained.

Correction and independent QA run concurrently in six-shot batches. Correction
may change allowlisted visual descriptions and per-shot presence/state, while
shot boundaries, ASR, dialogue tracks and sound data remain untouched. Subtitle
and title corrections require exact independent visual confirmation. Every old
finding and every new first-review defect needs an explicit cited disposition.
Generic audio/unsampled-motion limits cannot waive identity or physical defects.
Original reports and descriptions remain in the revision audit.

Remaining-issue runs use four-shot batches with the configured shared Avis
concurrency. They preserve previously verified shots when the source binding
still matches, without repeating the completed global layer/identity passes.
Each target receives up to two neighboring shots on either side, within eight
seconds, plus canonical identity anchors. Neighbors can establish an identity
across a reverse angle; only evidence from the target shot can establish its
presence there. Scene boundaries are explicit and clothing alone is insufficient.
Contextual identity links need current and selected context citations and a
separate reviewer's confirmation. New identities cannot inherit established
identity status from these links.

Active issues have stable IDs and a lossless history audit. Exact repeated
questions are grouped; different visual fields and conflicting claims remain
separate. Proven aliases and misplaced citation roles can be repaired
mechanically, but this never grants visual verification. QA receives the complete
relevant catalog even when a writer returns an empty list of new definitions.
The report/UI separates corrected-and-verified shots, unresolved visual facts,
technical issues, and insufficient evidence. Human acceptance remains a separate
action and retains the machine's original verdict.

QA receives an explicit question for each previously visible asset removed from
a proposal, including props visible briefly at frame edges. This does not force
the old draft to be true: the independent reviewer must justify the new coverage
against own-shot images. Container contents and holders are shown alongside the
related objects' current positions/states; historical origin is not containment.

An existing source-bound analysis can recheck selected shots with
`POST /api/automation/videos/{id}/refine` and `{"shots":[1,2]}`. Other open issues
remain open, including global findings; unchanged shots are retained. Invalid
or stale source bindings require a normal full/remaining pass. This supports
targeted corrections without paying to recheck every other unresolved shot.

Each batch permits one repair round. Useful extra-frame requests share a budget
that scales with film length; decoder concurrency is bounded independently of
Avis concurrency. A valid source time with an invalid crop can safely fall back
to the entire source frame, with an audit note; timestamps and pixel coordinates
are never guessed. Unresolved or invalid final requests still block.

The separate `source_refinement.v1.json` journal binds source/frame bytes, model
IDs, code policy, source descriptions, inventory and findings. Successful stages
resume; provider failures do not certify the film or overwrite the editable
source draft. A completed result is not human acceptance. Earlier boards must be
refreshed from the updated source/cast before their prompts can be current.

Existing linked plates for identities that merge are preserved. Explicitly
retired duplicate or graphic cast entries are retained in `retired_source_assets`
with their designs/media, instead of being sent for physical-reference generation.
Unrelated manually created assets are unaffected.


### Resume remaining source issues

The “Sửa & đối chiếu” action now rechecks only unresolved shots when the prior
source video, evidence bytes, model IDs, inventory and source-shot digests still
match. Changed source inputs fall back to a full pass. Global identity/profile
changes expand the requested scope to affected appearances and dependencies.
The report records both processed shots and retained verified shots with prior
policy provenance; this is not human acceptance.

Independent QA receives one current inventory/description view. Finding rows
include host-owned allowed decisions, and protocol failures preserve their
original concrete question on resume. Optional duplicate-identity comparisons
that remain uncertain keep both IDs and an audited pending alias note; they do
not invalidate every established appearance of both people. Actual per-shot
identity/presence/evidence problems remain blocking. Entity candidate retrieval
also uses repeated missing-ID observations to propose image comparisons; two
independent image verdicts and all co-occurrence/dependency guards still apply.

Verified source appearances pass unchanged through production/board coverage
hashes. Screen graphics are separate from physical reference assets and remain
available as source metadata.


When a source refinement changes an already adapted shot, the existing draft is
preserved with a stale-source marker. Adapt regenerates only those marked shots
when the rules and glossary are unchanged. Failed regeneration keeps the old
editable draft and its marker. A new board cannot use stale adaptations; exports
label affected shots and omit their stale overlays. Unchanged legacy adaptations
are not invalidated merely because this feature was introduced.


### Bounded protocol QA recovery

Each model-facing finding exposes one current top-level finding ID; nested
historical IDs are removed from the prompt, while the complete history stays in
the host audit. Known, previously verified identity aliases are canonicalized
before interpreting new model output, so an old ID cannot create a duplicate.

If final source QA still returns unknown/missing disposition IDs, one additional
independent QA pass checks only affected shots. It re-reads the current source
frames, inventory and descriptions and cannot rewrite any fact or accept a
report. Own-shot evidence, structural uncertainty, missing rows and unknown IDs
remain strict blockers. The pass is checkpointed and bounded to one new check
per batch; completed uncertainty is not retried indefinitely. The internal
POST /api/automation/videos/{id}/recheck action resumes that QA-only step for an
existing source-bound refinement. No source video is generated by these actions.

The QA-only endpoint also accepts `{"shots":[1,2]}` for a fresh check of selected
current facts, including technical findings left by an earlier proposal. It
cannot edit inventory or descriptions, and unselected/global blockers remain.
Selected-shot requests are part of the checkpoint key; a previous selection
cannot suppress a requested check of other shots.
