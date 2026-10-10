"""Wardrobe states are outfits, not camera views; a scene keeps them until the frames show a change."""
from flowboard.services.video_analyzer import wardrobe_checks as wc

MAID = {'key': 'maid', 'label': 'Maid uniform', 'look': 'Identity unchanged; hair in a bun.',
        'wardrobe': 'Dark maid uniform with white collar and a white apron.'}
WET_MAID = {'key': 'wet-maid', 'label': 'Wet maid uniform', 'look': 'Identity unchanged; hair in a bun.',
            'wardrobe': 'Dark maid uniform with white collar; apron visibly wet and stained.'}
BATH = {'key': 'bath', 'label': 'In the bath', 'look': 'Identity unchanged; wet hair slicked back.',
        'wardrobe': 'No garment visible; bare shoulders above the foam.'}
LILY = {'id': 'lily', 'kind': 'character', 'name': 'Lily', 'description': 'Young woman with dark hair.',
        'states': [MAID, WET_MAID, BATH]}


def row(scene, state, observed='', changes=()):
    return {'scene_id': scene, 'character_states': {'lily': state},
            'asset_presence': [{'asset_id': 'lily', 'visibility': 'visible', 'state': observed}],
            'wardrobe_changes': list(changes)}


def codes(rows, **kw):
    return [(i['code'], i.get('shot')) for i in wc.shot_issues(rows, {'lily': LILY}, **kw)]


def test_a_flip_by_view_inside_one_scene_needs_a_visible_change():
    rows = [(1, row('suite', 'maid')), (2, row('suite', 'wet-maid')), (3, row('suite', 'maid'))]
    assert codes(rows) == [('unexplained_wardrobe_change', 2), ('unexplained_wardrobe_change', 3)]


def test_a_recorded_change_and_a_new_scene_are_fine():
    spill = {'asset_id': 'lily', 'from_state': 'maid', 'to_state': 'wet-maid', 'basis': 'Wine spills on her apron.'}
    rows = [(1, row('lobby', 'maid')), (2, row('lobby', 'wet-maid', changes=[spill])), (3, row('bathroom', 'bath'))]
    assert codes(rows) == []
    # A change with an empty basis is not a record of anything.
    rows[1] = (2, row('lobby', 'wet-maid', changes=[{**spill, 'basis': ' '}]))
    assert codes(rows) == [('unexplained_wardrobe_change', 2)]


def test_only_shots_of_the_batch_are_reported_but_earlier_ones_give_the_scene():
    rows = [(1, row('lift', 'wet-maid')), (2, row('lift', 'wet-maid')), (3, row('lift', 'maid'))]
    assert codes(rows, check_from=3) == [('unexplained_wardrobe_change', 3)]
    assert codes(rows[:2], check_from=3) == []


def test_a_person_described_wet_gets_the_closest_wet_outfit_suggested():
    rows = [(1, row('suite', 'maid', 'Dark maid uniform and white apron appear wet and glossy.'))]
    found = wc.shot_issues(rows, {'lily': LILY})
    assert [i['code'] for i in found] == ['state_contradicts_observation']
    assert 'wet-maid is' in found[0]['message']
    # Saying it is not wet asserts nothing.
    assert codes([(1, row('suite', 'maid', 'Uniform is not wet; apron neat.'))]) == []


def test_loose_hair_contradicts_only_a_state_that_says_bun():
    plain = {'key': 'coat', 'label': 'Coat', 'look': 'Identity unchanged.', 'wardrobe': 'Green coat.'}
    loose = {'key': 'coat-loose', 'label': 'Coat, hair down', 'look': 'Hair worn down.', 'wardrobe': 'Green coat.'}
    anna = {'id': 'lily', 'kind': 'character', 'description': 'Woman with dark hair.', 'states': [plain, loose]}
    rows = [(1, row('park', 'coat', 'Her hair is loose over her shoulders.'))]
    assert wc.shot_issues(rows, {'lily': anna}) == []
    assert [i['code'] for i in wc.shot_issues([(1, row('suite', 'maid', 'Hair hanging loose, wet.'))],
                                              {'lily': {**LILY, 'states': [MAID, {**BATH, 'look': 'Hair worn loose.'}]}})] \
        == ['state_contradicts_observation']


def test_another_hair_colour_than_the_profile_is_flagged_as_maybe_another_person():
    rows = [(1, row('kitchen', 'maid', 'Woman with straight blonde hair in a maid uniform.'))]
    assert [i['code'] for i in wc.shot_issues(rows, {'lily': LILY})] == ['identity_contradicts_profile']
    rows = [(1, row('kitchen', 'maid', 'Dark-brown hair in a bun.'))]
    assert wc.shot_issues(rows, {'lily': LILY}) == []


def test_a_crop_is_not_a_state_unless_it_is_all_the_person_has():
    legs = {'key': 'bare-legs', 'label': 'Bare legs', 'look': 'Only lower legs and bare feet are visible.',
            'wardrobe': 'No garment visible in the cropped view.'}
    person = {'id': 'man', 'kind': 'character', 'states': [MAID, legs]}
    assert [i['code'] for i in wc.new_state_issues(person, {'bare-legs'})] == ['partial_view_state']
    blurred = {'key': 'blur', 'wardrobe': 'Dark clothing, details indistinct.', 'look': 'Seen from behind.'}
    assert wc.new_state_issues({'id': 'observer', 'states': [blurred]}, {'blur'}) == []


def test_an_exact_repeat_of_an_outfit_and_look_is_a_duplicate_state():
    again = {**MAID, 'key': 'maid-again', 'label': 'Maid uniform (close)'}
    issues = wc.new_state_issues({'id': 'lily', 'states': [MAID, again]}, {'maid-again'})
    assert [i['code'] for i in issues] == ['duplicate_state'] and 'reuse maid' in issues[0]['message']


def test_audit_counts_problems_in_a_finished_catalog():
    inventory = {'assets': [LILY], 'shots': {'1': row('a', 'maid'), '2': row('a', 'bath')},
                 'catalog_notes': [{'code': 'x'}]}
    report = wc.audit(inventory)
    assert report['counts'] == {'unexplained_wardrobe_change': 1} and report['states_per_character'] == {'lily': 3}
