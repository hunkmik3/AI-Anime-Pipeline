"""Print the deterministic shotlist audit of an analysed video (no model is called).

    cd agent && .venv/bin/python scripts/shotlist_audit.py <video id or prefix> [--json]

Reads the analysis from the database; when the one-pass preparation file exists in the
video's work dir, its prepared analysis, cast and adaptation are measured instead (that
is the shotlist the film is made from). Nothing is written.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

from sqlmodel import select  # noqa: E402

from flowboard.config import STORAGE_DIR  # noqa: E402
from flowboard.db import get_session  # noqa: E402
from flowboard.db.models import VideoAnalysis  # noqa: E402
from flowboard.services.video_analyzer import shotlist_audit  # noqa: E402


def load(prefix: str):
    with get_session() as s:
        rows = [r for r in s.exec(select(VideoAnalysis)).all() if str(r.id).startswith(prefix)]
        if len(rows) != 1:
            raise SystemExit(f"{len(rows)} analyses match {prefix!r}; give more of the id.")
        row = rows[0]
        analysis, adaptation, cast, name = dict(row.analysis or {}), dict(row.adaptation or {}), dict(row.cast or {}), row.name
    prepared = Path(STORAGE_DIR) / "video_analysis" / str(row.id) / "one-pass-production.json"
    if prepared.exists():
        saved = json.loads(prepared.read_text())
        analysis, adaptation, cast = saved["analysis"], saved.get("adaptation"), saved.get("cast")
    return str(row.id), name, analysis, adaptation, cast, prepared.exists()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    vid, name, analysis, adaptation, cast, prepared = load(argv[1])
    report = shotlist_audit.report(analysis, adaptation, cast)
    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    d, v, sp, w = report["dialogue"], report.get("voices") or {}, report.get("speakers") or {}, report.get("wardrobe") or {}
    print(f"{name} ({vid[:8]}) — {'prepared one-pass shotlist' if prepared else 'analysis'}")
    print(f"  shots {d['shots']} | with captions {d['shots_with_captions']} | with lines {d['shots_with_lines']} | "
          f"short (<1 s) {d['short_shots']}")
    print(f"  captions: {'verbatim' if d['captions_verbatim'] else 'not verbatim'} (match {d['caption_language_match']}) | "
          f"coverage {d['caption_coverage']}")
    for m in d["missing_caption_lines"]:
        print(f"    missing  shot {m['shots']}: {m['text']!r}" + (f"  [{m['reason']}]" if m.get("reason") else ""))
    if d["speech_shots_without_lines"]:
        print(f"  captioned shots with no line: {d['speech_shots_without_lines']}")
    for o in d["overlong_shots"]:
        print(f"    overlong shot {o['shot']}: {o['speech_s']} s of speech in {o['shot_s']} s")
    for m in d["merged_turn_lines"]:
        print(f"    merged turns from shot {m['first_shot']} (captions in {m['caption_shots']}): {m['text']!r}")
    if sp:
        print(f"  speakers: {sp.get('lines_with_speaker_id')}/{sp.get('lines')} lines bound to a character id")
    if v.get("status") == "measured":
        print(f"  voices: {v['voices']} | confidence {v['confidence']} | listening {v.get('listening')} | "
              f"changed by voice {v.get('speakers_changed_by_voice')}")
        for x in v.get("voices_given_to_several_people") or []:
            print(f"    voice {x['voice']} given to {x['speakers']}")
    if w:
        print(f"  wardrobe findings {w.get('counts')} | states per character {w.get('states_per_character')} | "
              f"notes left by the catalog {w.get('notes_left_by_catalog')}")
        for x in w.get("examples") or []:
            print(f"    {x['code']}" + (f" shot {x['shot']}" if x.get('shot') is not None else "") + f": {x['message'][:140]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
