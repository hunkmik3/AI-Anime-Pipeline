"""Manga colorizer — read-first, color-locked chapter colorization (Giant Studio).

Two passes over a chapter of B&W pages, with a human checkpoint between:

  PASS 1  build_bible : a Gemini vision model (via Atrium) reads the whole chapter
                        → a "bible" (characters/outfits/props/scenes/per-page cast)
                        with fixed hex colours. Human reviews/edits it.
  PASS 2  colorize    : per page, a COLOR-LOCK prompt is built from the bible and a
                        Seedream engine renders it, so every page reads the SAME
                        fixed palette → cross-page consistency.

Integrated as a canvas SEQUENCE (Shot ``kind="colorize"``): one sequence = one
chapter, its pages/bible/sheets/outputs live in ``Shot.workflow_metadata``.

  * bible.py   — the validated colour bible (pydantic).
  * reader.py  — PASS 1 (Gemini via Atrium, pages hosted by the app's /thumb URL).
  * prompt.py  — the per-page COLOR-LOCK prompt + character-sheet prompt.
  * engine     — ``flowboard.services.image.seedream`` (DanceSee B2B / Avis).
"""
