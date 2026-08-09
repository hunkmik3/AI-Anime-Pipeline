"""Comic names are built, not typed.

Six comics had been named by hand and four were missing the studio's prefix —
`26001_MAGMEL` beside `GCSA_26007_BACKWARDS-HOUSE`. That is the same drift the
batch names were given a generator to stop ("Quân", "quan",
"26004_UL-X-MEN_Quân" in one comic), one tier up: the number appears in exported
folder names and in what people say to each other, so a comic whose name was
typed differently is a comic two people cannot agree they are discussing.

A person supplies the TITLE. The prefix and the number are the studio's.
"""
from __future__ import annotations

from flowboard.db import get_session
from flowboard.services import panel_service as ps


def _slate(name="Global Comix"):
    with get_session() as s:
        return ps.create_project(s, name).id


def _make(pid, title):
    with get_session() as s:
        return ps.create_series(s, pid, title).name


def test_a_title_becomes_a_full_name(client):
    pid = _slate()
    assert _make(pid, "MAGMEL") == "GCSA_26001_MAGMEL"


def test_numbers_run_on_within_a_slate(client):
    pid = _slate()
    assert [_make(pid, t) for t in ("A", "B", "C")] == [
        "GCSA_26001_A", "GCSA_26002_B", "GCSA_26003_C",
    ]


def test_a_typed_title_is_normalised(client):
    """Spaces and case are the two things people differ on, so neither survives
    into an identifier that ends up in a folder name."""
    pid = _slate()
    assert _make(pid, "aura 6 tower") == "GCSA_26001_AURA-6-TOWER"


def test_a_name_already_in_the_convention_is_left_alone(client):
    """Re-saving a comic must not renumber it, and pasting its full name must
    not produce GCSA_26009_GCSA_26001_MAGMEL."""
    pid = _slate()
    assert _make(pid, "GCSA_26001_MAGMEL") == "GCSA_26001_MAGMEL"


def test_numbering_continues_past_a_pasted_name(client):
    pid = _slate()
    _make(pid, "GCSA_26050_IMPORTED")
    assert _make(pid, "NEXT") == "GCSA_26051_NEXT"


def test_a_deleted_comics_number_is_not_handed_to_the_next(client):
    """The number is in exported folders and in what people say out loud.
    Reusing it makes two different comics share an identifier."""
    pid = _slate()
    _make(pid, "FIRST")
    second = _make(pid, "SECOND")
    with get_session() as s:
        sid = next(x.id for x in ps.list_series(s, pid) if x.name == second)
        ps.delete_series(s, sid)
    assert _make(pid, "THIRD") == "GCSA_26003_THIRD"


def test_each_slate_numbers_independently(client):
    a, b = _slate("Global Comix"), _slate("Other House")
    assert _make(a, "X") == "GCSA_26001_X"
    assert _make(b, "Y") == "GCSA_26001_Y"


def test_the_title_can_be_read_back_out(client):
    assert ps.series_title_of("GCSA_26001_MAGMEL") == "MAGMEL"
    # …and anything not in the convention is all title.
    assert ps.series_title_of("just a name") == "just a name"


def test_batch_names_carry_the_new_comic_name(client):
    """Batch names are built from the comic's, so the two cannot be allowed to
    describe different things."""
    pid = _slate()
    with get_session() as s:
        comic = ps.create_series(s, pid, "MAGMEL")
        ch = ps.create_chapter(s, comic.id, "Chapter 1")
        b = ps.create_batch(s, ch.id, None)
        assert b.name.startswith(ps.batch_name_prefix(s, ch.id))
        assert "GCSA-26001-MAGMEL" in b.name


def test_a_pasted_number_raises_the_mark(client):
    """A name brought in from outside still spends its number. Otherwise the
    next generated comic is numbered BELOW one already on the slate — not a
    collision, but a numbering that runs backwards, which reads worse than a
    gap."""
    pid = _slate()
    _make(pid, "FIRST")                 # 26001
    _make(pid, "GCSA_26050_IMPORTED")   # brought in
    assert _make(pid, "AFTER") == "GCSA_26051_AFTER"


def test_a_lower_pasted_number_does_not_move_the_mark_backwards(client):
    pid = _slate()
    _make(pid, "GCSA_26090_HIGH")
    _make(pid, "GCSA_26002_OLD")        # older, lower
    assert _make(pid, "NEXT") == "GCSA_26091_NEXT"
