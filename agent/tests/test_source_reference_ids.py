from flowboard.services.video_analyzer.source_reference_ids import normalize_reference_ids, rebind_character_anchors


def test_typo_in_display_prefix_keeps_exact_host_identity_and_leaves_prose_untouched():
    good = "wave-asset-prop_phone-1234567890abcdef1234"
    bad = "wave-asset-prop-pohne-1234567890abcdef1234"
    value = {"assets": [{"id": bad, "description": bad}],
             "shots": {"1": {"asset_presence": [{"asset_id": bad, "holder_id": "person"}]}},
             "context_links": [{"asset_id": bad}], "contains_ids": [bad]}
    out, audit = normalize_reference_ids(value, [good, "person"])
    assert out["assets"][0] == {"id": good, "description": bad}
    assert out["shots"]["1"]["asset_presence"][0]["asset_id"] == good
    assert out["context_links"][0]["asset_id"] == good
    assert out["contains_ids"] == [good]
    assert value["assets"][0]["id"] == bad
    assert len(audit) == 4 and not any(a["authorizes_identity_or_presence"] for a in audit)


def test_similar_name_changed_token_and_ambiguous_suffix_never_merge():
    key = "wave-asset-prop_phone-1234567890abcdef1234"
    for known, bad in [([key], "wave-asset-prop_phone-1234567890abcdef1235"),
                       ([key, "wave-asset-prop_other-1234567890abcdef1234"],
                        "wave-asset-typo-1234567890abcdef1234"),
                       ([key], "prop_phone")]:
        out, audit = normalize_reference_ids({"asset_id": bad}, known)
        assert out == {"asset_id": bad} and not audit


def test_anchor_reassignment_requires_independently_verified_presence():
    inv = {'assets': [{'id': 'child', 'kind': 'character', 'evidence_ids': ['a', 'b']}],
           'shots': {'1': {'asset_presence': [{'asset_id': 'child', 'visibility': 'visible', 'evidence_ids': ['a']}]},
                     '2': {'asset_presence': [{'asset_id': 'adult', 'visibility': 'visible', 'evidence_ids': ['b']}]}}}
    evidence = [{'id': 'a', 'shot': 1}, {'id': 'b', 'shot': 2}]
    unchanged, audit = rebind_character_anchors(inv, evidence, {1})
    assert unchanged == inv and not audit
    corrected, audit = rebind_character_anchors(inv, evidence, {1, 2})
    assert corrected['assets'][0]['evidence_ids'] == ['a']
    assert inv['assets'][0]['evidence_ids'] == ['a', 'b']
    assert corrected['shots'] == inv['shots']  # This repair never assigns identities.
