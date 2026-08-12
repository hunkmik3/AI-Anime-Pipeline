

# ── the editor ──────────────────────────────────────────────────────────────


def test_an_editor_may_pull_material_and_hand_a_cut_back():
    """The job the role exists for: take the raw clips out, bring one file back."""
    from flowboard.services import permissions as p

    for cap in ("material.pull", "cut.submit", "cut.annotate"):
        assert p.role_allows(p.EDITOR, cap), cap


def test_an_editor_generates_nothing():
    """They cut, they do not make. An editor with canvas.write would be an artist
    with extra steps, and the point of the role is that it is a different job."""
    from flowboard.services import permissions as p

    assert not p.role_allows(p.EDITOR, "canvas.write")
    assert not p.role_allows(p.EDITOR, "sequence.create")
    assert not p.role_allows(p.EDITOR, "series.create")
    assert p.role_allows(p.EDITOR, "canvas.read"), "they still have to see the work"


def test_a_viewer_does_not_get_the_raw_material():
    """The trap in ranking editor beside viewer.

    Editor is not "more than viewer" — it is a different job — so it sits at the
    same rank, and a plain rank comparison therefore handed every viewer on the
    project the entire raw material of the series. Named roles decide these three,
    not the ladder.
    """
    from flowboard.services import permissions as p

    assert p._RANK[p.EDITOR] == p._RANK[p.VIEWER], "premise: same rank"
    for cap in ("material.pull", "cut.submit", "cut.annotate"):
        assert not p.role_allows(p.VIEWER, cap), cap


def test_a_pm_can_cover_an_empty_editor_seat():
    """A studio of four should not be blocked because nobody holds the role."""
    from flowboard.services import permissions as p

    for role in (p.ARTIST, p.PRODUCER, p.ADMIN):
        assert p.role_allows(role, "material.pull"), role


def test_editor_is_assignable_and_survives_a_round_trip():
    """`normalize_role` falls back to ARTIST for anything unknown — so a role that
    is not in PROJECT_ROLES is silently promoted to one that can generate."""
    from flowboard.services import permissions as p

    assert p.EDITOR in p.PROJECT_ROLES
    assert p.normalize_role("editor") == p.EDITOR
    assert p.normalize_role("Editor ") == p.EDITOR
