"""Pure board-save regressions; may run with pytest --noconftest (no database)."""
from copy import deepcopy
import json

import pytest

from flowboard.services.board_roundtrip import preserve_numeric_representation
from flowboard.services import production_manifest


def test_browser_autosave_keeps_production_manifest_hash_with_canvas_edit():
    stored = {
        "nodes": [{
            "id": "seq:c1",
            "position": {"x": 0.0, "y": 20.0},
            "data": {
                "kind": "sequence",
                "sequence": {"key": "c1"},
                "shots": [{
                    "source_shot": 1,
                    "scene_id": "hall",
                    "source_start": 0.0,
                    "source_end": 20.0,
                    "duration_s": 20.0,
                }],
            },
        }],
    }
    # Equivalent to the browser's JSON.parse/JSON.stringify numeric roundtrip.
    incoming = json.loads(json.dumps(stored).replace("0.0", "0"))
    incoming["nodes"][0]["position"]["x"] = 100
    original = deepcopy(incoming)
    before = production_manifest.build(stored, "film")
    assert production_manifest.build(incoming, "film")["version"] != before["version"]

    saved = preserve_numeric_representation(incoming, stored)

    assert production_manifest.build(saved, "film")["version"] == before["version"]
    assert saved["nodes"][0]["position"]["x"] == 100
    assert type(saved["nodes"][0]["data"]["shots"][0]["duration_s"]) is float
    assert incoming == original
    assert type(incoming["nodes"][0]["data"]["shots"][0]["duration_s"]) is int
    assert stored["nodes"][0]["position"]["x"] == 0.0


@pytest.mark.parametrize("incoming,stored", [(20, 20.0), (20.0, 20), (0, -0.0)])
def test_equal_numbers_keep_stored_serialization(incoming, stored):
    result = preserve_numeric_representation({"value": [incoming]}, {"value": [stored]})
    assert json.dumps(result) == json.dumps({"value": [stored]})


@pytest.mark.parametrize("incoming,stored", [
    (20.000000000000004, 20.0),
    (21, 20.0),
    (True, 1),
    (1, True),
    (False, 0.0),
    (0, False),
    ("20", 20.0),
    (None, 0),
])
def test_real_value_and_type_edits_win(incoming, stored):
    result = preserve_numeric_representation({"value": incoming}, {"value": stored})
    assert result["value"] == incoming
    assert type(result["value"]) is type(incoming)


def test_structural_edits_and_new_values_are_preserved():
    stored = {"deleted": 1.0, "values": [0.0, 2.0, 3.0], "shape": {"old": 1.0}}
    incoming = {"values": [0, 4], "shape": [1], "added": {"value": 2}}
    result = preserve_numeric_representation(incoming, stored)
    assert json.dumps(result) == '{"values": [0.0, 4], "shape": [1], "added": {"value": 2}}'
    assert preserve_numeric_representation([0, 2, 3], [0.0]) == [0.0, 2, 3]


def test_real_shot_edit_changes_production_manifest_hash():
    stored = {"nodes": [{"id": "seq:c1", "data": {
        "kind": "sequence", "sequence": {"key": "c1"},
        "shots": [{"source_shot": 1, "scene_id": "hall", "duration_s": 20.0}],
    }}]}
    incoming = deepcopy(stored)
    incoming["nodes"][0]["data"]["shots"][0]["duration_s"] = 21
    saved = preserve_numeric_representation(incoming, stored)
    assert saved["nodes"][0]["data"]["shots"][0]["duration_s"] == 21
    assert production_manifest.build(saved)["version"] != production_manifest.build(stored)["version"]
