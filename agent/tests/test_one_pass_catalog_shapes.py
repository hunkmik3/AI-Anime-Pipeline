from copy import deepcopy

import pytest

from flowboard.services.video_analyzer import one_pass_film as film


def catalog():
    return {'assets': [{'id': 'room', 'kind': 'environment', 'name': 'Room',
                        'description': 'A quiet room', 'evidence_ids': ['frame-1']}],
            'scenes': [{'id': 'home', 'shot_ids': [1], 'present_asset_ids': ['room']}],
            'shots': {'1': {'scene_id': 'home', 'environment_key': 'room',
                             'evidence_ids': ['frame-1'], 'asset_presence': [],
                             'character_states': {}, 'speakers': [], 'findings': []}},
            'findings': []}


def malformed_catalogs():
    yield None
    yield []
    for key in ('assets', 'scenes', 'shots', 'findings'):
        for value in (None, 'invalid', 42):
            bad = catalog()
            bad[key] = value
            yield bad
    for value in (None, 'invalid', [], 42):
        bad = catalog()
        bad['shots']['1'] = value
        yield bad
    for key in ('asset_presence', 'character_states', 'speakers', 'departures', 'transitions'):
        bad = catalog()
        bad['shots']['1'][key] = 42
        yield bad
    bad = catalog()
    bad['assets'][0]['states'] = None
    yield bad
    bad = catalog()
    bad['shots']['1']['environment_key'] = []
    yield bad


@pytest.mark.parametrize('bad', list(malformed_catalogs()))
def test_malformed_shapes_return_repair_findings_without_mutating_reply(bad):
    original = deepcopy(bad)
    _, issues = film.normalize(bad, [{'shot': 1}], {'assets': [], 'scenes': [], 'shots': {}}, [])
    assert issues and all(issue['code'] == 'invalid_catalog_shape' for issue in issues)
    assert bad == original


@pytest.mark.asyncio
async def test_malformed_shot_gets_one_local_repair_and_keeps_original_reply(tmp_path, monkeypatch):
    good = catalog()
    bad = deepcopy(good)
    bad['shots']['1'] = None
    calls = []

    async def call(payload, *args, **kwargs):
        calls.append((payload, kwargs))
        return good if kwargs.get('repair') else bad

    monkeypatch.setattr(film, '_call', call)
    monkeypatch.setattr(film.inv, '_initial_evidence', lambda *args: [
        {'id': 'frame-1', 'shot': 1, 'frame': 'frame.jpg'}])
    analysis = {'video': {'duration': 4., 'fps': 30.}, 'shots': [
        {'shot': 1, 'start': 0., 'end': 4., 'source': {'shot_size': 'MS', 'action': 'Empty room.'},
         'dialogue_lines': []}]}
    prepared, _, _ = await film.prepare(analysis, tmp_path, 'Film', {})
    assert prepared['source_verification']['structural_checks_passed']
    assert len(calls) == 2 and calls[1][1]['repair'] is True
    assert calls[1][0]['previous_output']['shots']['1'] is None
    assert calls[1][0]['repair_only_these_findings'][0]['code'] == 'invalid_catalog_shape'


@pytest.mark.asyncio
async def test_repeated_malformed_container_stops_after_one_repair(tmp_path, monkeypatch):
    calls = []

    async def call(*args, **kwargs):
        calls.append(kwargs)
        return {'shots': []}

    monkeypatch.setattr(film, '_call', call)
    monkeypatch.setattr(film.inv, '_initial_evidence', lambda *args: [
        {'id': 'frame-1', 'shot': 1, 'frame': 'frame.jpg'}])
    analysis = {'video': {'duration': 4., 'fps': 30.}, 'shots': [
        {'shot': 1, 'start': 0., 'end': 4., 'source': {'action': 'Empty room.'}}]}
    with pytest.raises(ValueError, match='One-pass catalog needs attention'):
        await film.prepare(analysis, tmp_path, 'Film', {})
    assert len(calls) == 2 and calls[1]['repair'] is True
    assert (tmp_path / 'one-pass-catalog-findings.json').exists()
