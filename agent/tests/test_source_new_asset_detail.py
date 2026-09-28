"""Additional-frame correction retains the full catalog and tentative assets."""
import copy
import hashlib

import pytest

from flowboard.services.video_analyzer import source_inventory as inv, source_refinement as refine
from tests.test_source_context_refinement import _install, _run
from tests.test_source_refinement import _case


def test_detail_empty_asset_delta_retains_new_definition_and_full_catalog(tmp_path, monkeypatch):
    case = _case(tmp_path)
    frozen = {a['id']: copy.deepcopy(a) for a in case[2]['scene_inventory']['assets']}
    writer_calls = []
    frame_calls = []
    new_ids = []

    async def frames(entry, stage, requests, batch, video, work_dir, fps, budget, save):
        frame_calls.append((stage, copy.deepcopy(requests)))
        assert stage == 'edit_frames_0' and requests[0]['shot'] == 1
        # Reuse a fixture image as the mocked extraction result; no decoder runs.
        evidence = copy.deepcopy(case[2]['source_verification']['evidence'][0])
        evidence.update(id='shot-1-extra-detail', timestamp_s=0.5, sampling='requested_frame')
        evidence['sha256'] = hashlib.sha256((work_dir / case[2]['shots'][0]['frames'][0]).read_bytes()).hexdigest()
        return [evidence]

    def writer(data, payload):
        writer_calls.append(copy.deepcopy(payload))
        if len(writer_calls) == 1:
            data['assets'] = [{'id': 'new-brooch', 'kind': 'prop', 'name': 'Observed brooch',
                'description': 'A separate silver brooch.', 'reference_required': True,
                'member_ids': [], 'depends_on_asset_ids': [], 'evidence_ids': ['shot-1-frame-1']}]
            data['shots']['1']['asset_presence'].append({'asset_id': 'new-brooch',
                'visibility': 'visible', 'position': 'Foreground', 'state': 'On the counter.',
                'contains_ids': [], 'evidence_ids': ['shot-1-frame-1']})
            data['review_requests'] = [{'shot': 1, 'timestamp_s': 0.5, 'crop': None,
                'reason': 'Inspect the distinct silver object more closely.'}]
        else:
            assert len(writer_calls) == 2
            catalog = {a['id']: a for a in payload['draft_inventory']['assets']}
            assert {key: catalog[key] for key in frozen} == frozen
            definition = next(a for a in catalog.values() if a['name'] == 'Observed brooch')
            new_ids.append(definition['id'])
            assert len(catalog) == len(frozen) + 1
            assert data['assets'] == []  # The detail writer returns an empty valid delta.
            presence = next(p for p in data['shots']['1']['asset_presence']
                if p['asset_id'] == definition['id'])
            presence['evidence_ids'] = ['shot-1-extra-detail']

    def checker(data, payload):
        catalog = {a['id']: a for a in payload['proposed_inventory']['assets']}
        assert {key: catalog[key] for key in frozen} == frozen
        assert new_ids[0] in catalog
        assert not any(f['code'] == 'unknown_asset' for f in payload['structural_findings'])
        assert new_ids[0] not in payload['temporal_context']['canonical_asset_ids']

    _install(monkeypatch, writer=writer, checker=checker)
    monkeypatch.setattr(inv, '_stage_frames', frames)
    result = _run(case)
    assert result['source_verification']['status'] == 'verified', '\n'.join(f['message'] for f in result['source_verification']['findings'])
    assert not result['source_verification']['retryable']
    assert len(writer_calls) == 2 and len(frame_calls) == 1
    catalog = {a['id']: a for a in result['scene_inventory']['assets']}
    assert {key: catalog[key] for key in frozen} == frozen
    assert len(catalog) == len(frozen) + 1 and new_ids[0] in catalog
    assert any(p['asset_id'] == new_ids[0] and p['evidence_ids'] == ['shot-1-extra-detail']
        for p in result['scene_inventory']['shots']['1']['asset_presence'])


@pytest.mark.parametrize('asset,expected', [
    ({'id': {'bad': 'id'}, 'kind': 'prop', 'evidence_ids': []}, 'invalid_asset'),
    ({'id': ['bad-id'], 'kind': 'prop', 'evidence_ids': []}, 'invalid_asset'),
    ({'id': 'bad-citation', 'kind': 'prop', 'evidence_ids': [{'bad': 'citation'}]}, 'invalid_evidence'),
])
def test_malformed_new_asset_fields_reach_normal_validation(tmp_path, asset, expected):
    case = _case(tmp_path)
    inventory = case[2]['scene_inventory']
    edit = {**copy.deepcopy(inventory), 'assets': [asset]}
    proposed, _ = refine._accumulate_local_assets(edit, inventory, '1-2', {}, {}, {})
    _, findings = inv._normalise(proposed, case[2]['shots'], inventory,
        case[2]['source_verification']['evidence'])
    assert any(f['code'] == expected for f in findings)
