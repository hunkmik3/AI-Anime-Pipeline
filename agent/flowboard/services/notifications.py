"""In-app release announcements shown in the header notification bell.

The bell shows ONE entry per release ("Update vX.Y.Z"); clicking it expands the
full changelog. Each change line has an ``audience``: ``"all"`` (everyone) or
``"admin"`` (admins only), so a non-admin never sees admin-only lines while
admins see the complete changelog.

Add a new release dict at the TOP of ``_RELEASES`` when you ship a version.
"""
from __future__ import annotations

_RELEASES: list[dict] = [
    {
        "version": "v3.0.0",
        "date": "2026-08-19",
        "changes": [
            {"audience": "all", "text": "Comic → Studio delivery is smarter — approved Giantflow "
             "panels now cross to Giant Studio in the correct reading order (by panel name, "
             "P001…P035), and an insert like “…P035-2” lands right after P035."},
            {"audience": "all", "text": "In a batch you can now add or delete a panel by hand, and a "
             "new “no-processing” (⏭) button sends a panel’s raw art straight to the studio without "
             "a generation step."},
            {"audience": "all", "text": "New video model: Seedance 2.5 — clips up to 30 seconds, "
             "480p / 720p, up to 30 reference images."},
            {"audience": "all", "text": "Skip content filter (B2B) on Seedance 2.0 / 2.5 — uses the "
             "DanceSee B2B path when the studio account is userType=B2B."},
            {"audience": "all", "text": "No more manual refresh — budgets, credits, work/review "
             "badges, the video gallery and admin views update live as things change."},
            {"audience": "all", "text": "A project’s “Generated videos” now filters to the series "
             "you select instead of showing the whole project."},
            {"audience": "admin", "text": "Employees & roles — one “Manage roles” panel per person: "
             "a company-wide role (user / manager / admin) plus a Giant Studio and a Giantflow role. "
             "The batch-assignee pool is built from your marked Giantflow staff."},
            {"audience": "admin", "text": "Archive control — freeze a series to view-only, or reopen "
             "an archived one, from Series & episodes."},
            {"audience": "admin", "text": "Export a person’s generation history to a spreadsheet "
             "(CSV) over any date range, from their activity view."},
        ],
    },
    {
        "version": "v1.0.2",
        "date": "2026-07-20",
        "changes": [
            {"audience": "all", "text": "Performance — projects with many sequences or clips now "
             "load much faster: video thumbnails are lightweight stills (first frame) instead of a "
             "full video per tile, and the canvas only renders what's currently on screen."},
            {"audience": "all", "text": "Fixed — extracting a continuity still frame from a clip "
             "works again."},
            {"audience": "all", "text": "In-app notifications — this bell now announces product "
             "updates, grouped per release; click an update to read its full changelog."},
            {"audience": "all", "text": "Download from the gallery — every clip in a project's "
             "'Generated videos' section now has a download button on its thumbnail."},
            {"audience": "all", "text": "Seedance 2.0 reference audio — attach a voice/audio "
             "reference (mp3 / wav) to guide a clip; supports multiple @audioN voices, with a "
             "~15-second total-audio check that warns before generating."},
        ],
    },
    {
        "version": "v1.0.1",
        "date": "2026-07-19",
        "changes": [
            {"audience": "all", "text": "New: project video gallery — every clip grouped by "
             "episode & sequence, with inline playback, prompt, settings and the reference "
             "images used."},
            {"audience": "all", "text": "Resolution fix — the resolution you pick when "
             "generating video is now the resolution you actually get (480p / 720p / 1080p)."},
            {"audience": "all", "text": "New video models: Seedance 1.5 Pro, 2.0 Fast, 2.0 Mini, "
             "1.0 Pro, 1.0 Pro Fast. Google Flow & BytePlus-direct removed; 4K disabled."},
            {"audience": "all", "text": "Added 4:3 and 1:1 aspect ratios; reference labels now go "
             "up to @image10."},
            {"audience": "all", "text": "Generation history clips are now playable and "
             "downloadable directly."},
            {"audience": "all", "text": "Fixed broken thumbnails in the Asset Library."},
            {"audience": "admin", "text": "Admin dashboard redesigned for mobile (tables become "
             "cards + a slide-in drawer menu); the cost breakdown shows the real resolution per "
             "generation."},
            {"audience": "admin", "text": "More reliable generation — Avis rate-limit "
             "(429 'gen limit reached') errors are retried automatically instead of failing."},
        ],
    },
]


def list_releases(is_admin: bool) -> list[dict]:
    """Releases visible to the caller (newest first). Each release carries only
    the change lines the caller is allowed to see; a release with nothing visible
    is dropped."""
    out = []
    for r in _RELEASES:
        changes = [
            c["text"]
            for c in r["changes"]
            if c.get("audience") == "all" or (c.get("audience") == "admin" and is_admin)
        ]
        if not changes:
            continue
        out.append(
            {
                "version": r["version"],
                "date": r["date"],
                "title": f"Update {r['version']}",
                "changes": changes,
            }
        )
    out.sort(key=lambda r: r.get("date", ""), reverse=True)
    return out
