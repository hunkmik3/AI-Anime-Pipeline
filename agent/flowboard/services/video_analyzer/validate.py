"""QA before anything is exported (guide §42-43).

Two kinds of finding, kept apart because they mean different things:

    error    the shotlist is structurally wrong — a gap in the timeline, shots
             out of order, a shot the adaptation silently dropped. Export must
             not pretend these are fine.
    warning  the shotlist is usable but a person should look — a shot the
             vision model could not read, a name that slipped past the
             glossary, a suspiciously short cut.

Every check is deterministic. None of them asks a model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Guide §83: anything this short is as likely an impact flash as a real cut.
SUSPICIOUS_SHOT_S = 0.25
# Guide §41: below this a field needs a human.
REVIEW_BELOW = 0.6


@dataclass
class Finding:
    level: str              # "error" | "warning"
    code: str
    message: str
    shot: Optional[int] = None

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "message": self.message, "shot": self.shot}


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def error(self, code: str, message: str, shot: Optional[int] = None) -> None:
        self.findings.append(Finding("error", code, message, shot))

    def warn(self, code: str, message: str, shot: Optional[int] = None) -> None:
        self.findings.append(Finding("warning", code, message, shot))

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": sum(f.level == "error" for f in self.findings),
            "warnings": sum(f.level == "warning" for f in self.findings),
            "findings": [f.as_dict() for f in self.findings],
        }


def check_timeline(shots: list[dict], duration: float, frame: float, report: Report) -> None:
    """Contiguous, numbered 1..N, starting at 0 and ending at the video's end."""
    if not shots:
        report.error("empty", "no shots")
        return

    for i, s in enumerate(shots, start=1):
        if s["shot"] != i:
            report.error("numbering", f"shot at position {i} is numbered {s['shot']}", s["shot"])
        if s["end"] <= s["start"]:
            report.error("zero_length", f"shot ends at or before it starts ({s['start']:.3f}→{s['end']:.3f})", s["shot"])

    if shots[0]["start"] > frame:
        report.error("late_start", f"first shot starts at {shots[0]['start']:.3f}s, not 0")

    for a, b in zip(shots, shots[1:]):
        gap = b["start"] - a["end"]
        if abs(gap) >= frame:
            kind = "gap" if gap > 0 else "overlap"
            report.error(kind, f"{kind} of {abs(gap):.3f}s between shot {a['shot']} and {b['shot']}", b["shot"])

    tail = duration - shots[-1]["end"]
    if abs(tail) >= frame:
        report.error("end_mismatch", f"last shot ends {tail:+.3f}s from the video's end ({duration:.3f}s)")


def check_analysis(shots: list[dict], report: Report) -> None:
    for s in shots:
        a = s.get("source")
        if not a:
            report.warn("not_analysed", "vision analysis missing — describe by hand or re-run", s["shot"])
            continue
        try:
            conf = float(a.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < REVIEW_BELOW:
            report.warn("low_confidence", f"vision confidence {conf:.2f}", s["shot"])
        if s["end"] - s["start"] < SUSPICIOUS_SHOT_S:
            report.warn(
                "suspicious_cut",
                f"{s['end'] - s['start']:.2f}s — possible impact flash; keep, or merge with a neighbour",
                s["shot"],
            )


def _mentions(text: str, name: str) -> bool:
    # Word-boundary match that also works for names with diacritics.
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) is not None


def _adapted_text(item: dict) -> str:
    parts: list[str] = []
    for key in ("title", "camera", "vfx", "edit_note", "adapted_description"):
        if isinstance(item.get(key), str):
            parts.append(item[key])
    for key in ("action", "performance", "avoid", "sfx"):
        parts += [x for x in item.get(key) or [] if isinstance(x, str)]
    for line in item.get("dialogue") or []:
        if isinstance(line, dict):
            parts += [str(line.get("who") or ""), str(line.get("line") or "")]
    return "\n".join(parts)


def check_adaptation(shots: list[dict], adapted: dict[int, dict], glossary: dict, report: Report) -> None:
    """Every shot adapted, and no source name surviving into the adapted text."""
    missing = [s["shot"] for s in shots if s["shot"] not in adapted]
    for n in missing:
        report.error("not_adapted", "adaptation dropped this shot", n)
    extra = sorted(set(adapted) - {s["shot"] for s in shots})
    for n in extra:
        report.error("invented_shot", "adaptation returned a shot number that does not exist", n)

    # A source name left in adapted text is the glossary leaking. Only names
    # that differ from their target count — "Tiên Minh" → "Tiên Minh" is a choice.
    leaks = {src: dst for table in glossary.values() for src, dst in (table or {}).items() if src and src != dst}
    for n, item in sorted(adapted.items()):
        text = _adapted_text(item)
        for src, dst in leaks.items():
            if _mentions(text, src) and not _mentions(dst, src):
                report.warn("glossary_leak", f'source name "{src}" left in adapted text (should be "{dst}")', n)


def _spoken(item: dict) -> list[str]:
    return [
        re.sub(r"[^\w\s]", "", str(d.get("line") or "").lower()).strip()
        for d in item.get("dialogue") or []
        if isinstance(d, dict) and d.get("line")
    ]


def check_dialogue(adapted: dict[int, dict], report: Report) -> None:
    """No line written twice.

    A subtitle persists across cuts, so this is the failure the dialogue track
    exists to prevent — and the cheapest place to notice it coming back."""
    recent: list[tuple[int, str]] = []
    for n, item in sorted(adapted.items()):
        for line in _spoken(item):
            words = line.split()
            if len(words) < 3:
                continue  # "Đi đi." really is said twice
            for earlier, said in recent[-6:]:
                shared = len(set(words) & set(said.split()))
                if line == said or shared >= max(3, int(0.7 * len(words))):
                    report.warn(
                        "dialogue_repeat",
                        f'line repeats shot {earlier:03d}: "{line[:60]}"',
                        n,
                    )
                    break
            recent.append((n, line))


def check_glossary(glossary: dict, report: Report) -> None:
    """One target per entity; two different entities never share a target."""
    for kind, table in glossary.items():
        by_target: dict[str, list[str]] = {}
        for src, dst in (table or {}).items():
            if not str(dst).strip():
                report.error("glossary_blank", f'{kind}: "{src}" has no target name')
                continue
            by_target.setdefault(str(dst).strip().casefold(), []).append(src)
        for target, sources in by_target.items():
            if len(sources) > 1:
                # Aliases of one person legitimately share a target; the entity
                # pass lists them, so this is only a prompt to check, not an error.
                report.warn("glossary_shared_target", f'{kind}: {", ".join(sources)} all map to "{target}" — same entity?')


def check_sequences(shots: list[dict], sequences: list[dict], report: Report) -> None:
    """Sequences are navigation only (guide §84) but must still tile the shotlist."""
    if not sequences:
        report.warn("no_sequences", "no sequence grouping")
        return
    n = len(shots)
    expected = 1
    for q in sequences:
        a, b = q.get("first_shot"), q.get("last_shot")
        if a != expected:
            report.warn("sequence_tiling", f'sequence "{q.get("title")}" starts at shot {a}, expected {expected}')
        expected = (b or expected) + 1
    if expected - 1 != n:
        report.warn("sequence_tiling", f"sequences end at shot {expected - 1}, shotlist has {n}")


def validate(
    shots: list[dict],
    *,
    duration: float,
    frame: float,
    sequences: Optional[list[dict]] = None,
    glossary: Optional[dict] = None,
    adapted: Optional[dict[int, dict]] = None,
) -> Report:
    report = Report()
    check_timeline(shots, duration, frame, report)
    check_analysis(shots, report)
    if sequences is not None:
        check_sequences(shots, sequences, report)
    if glossary is not None:
        check_glossary(glossary, report)
    if adapted is not None and glossary is not None:
        check_adaptation(shots, adapted, glossary, report)
    if adapted is not None:
        check_dialogue(adapted, report)
    return report
