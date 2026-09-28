"""Video-to-shotlist analyzer — the "reference video" entry into /automation.

A reference clip goes in; a timecoded, shot-by-shot breakdown comes out, and
can then be adapted into another world (anime, renamed cast, English sect and
place names) without touching the editorial structure.

The one rule everything here is built around, lifted straight from the
implementation guide: **the timeline is deterministic, the interpretation is
probabilistic.** Cuts and timecodes are measured by the machine — ffprobe,
PySceneDetect, frame statistics — and no model is ever allowed to move them.
Models only describe what is inside a shot the machine already bounded.

Layout, in pipeline order:

    probe.py        ffprobe → duration, fps, size, audio presence
    cuts.py         PySceneDetect + refinement (flash/impact false cuts merged)
    frames.py       2-5 keyframes per shot, sized for a vision model
    asr.py          spawns asr_worker.py — faster-whisper in its OWN process
    vision.py       per-shot analysis, cheap model first, strong model for hard shots
    adapt.py        entity pass, locked glossary, adaptation into the target world
    validate.py     timeline / count / glossary QA before anything is exported
    pipeline.py     orchestration + progress
    export.py       JSON (source of truth) and Markdown

Source analysis and adaptation are stored separately, never merged: re-styling
a film must not require watching it again.
"""
