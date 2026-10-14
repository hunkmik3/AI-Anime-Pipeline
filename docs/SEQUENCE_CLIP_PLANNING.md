# Sequence-aware reskin clip planning

New `reskin_v1` output boards use `video_analyzer/sequence_clips.py`. Existing
boards and submitted generations keep their original clip boundaries.

The planner reuses the source analysis and dramatic sequences. GPT through Avis
labels source-cut endpoints as complete sequences, complete beats, or continuing
action. Text calls use at most 40 owned shots with neighboring context, up to
three calls concurrently, and durable input-hash receipts. This is editorial
interpretation, not a second full-video analysis or source-fidelity approval.

A deterministic look-ahead planner chooses the grouping. It preserves every
source shot once, retains fractional timing, obeys the configured request cap,
and forbids cuts through a spoken utterance. ASR fragments ending with commas
are protected across the intervening pause unless speakers are known to differ.
Complete exchanges and sequence ends take priority over filling a fixed duration.
Long sequences split at internal beats; short sequences may remain short.

Each resulting sequence node carries `sequence_context` and `boundary_plan`.
The prompt summary explicitly marks full-sequence context as unspoken context,
with only the selected shots to be rendered. Actual source review, material
binding, utterance locks, and generated-output review remain separate requirements.

Failed/unknown semantic requests retain receipts and are not automatically billed
again. The deterministic fallback uses source sequence boundaries and speech
constraints, with `semantic_degraded` recorded. Impossible speech-safe grouping
raises a targeted error instead of truncating a sentence or silently exceeding
the generation duration cap. Boundary labels never clear source findings.

The 1011 continuation trial starts at 61.700 seconds. After source corrections,
the automatic plan selected shots 33–37, 38–42 and 43–47 (11.467, 10.566 and
10.934 seconds). No manual endpoint override was used. This trial validates the
grouping mechanics; it does not establish universally optimal clip lengths or
guarantee generated-video fidelity.
