import pytest

from flowboard.services.video_analyzer import board


def source_shots():
    return [{'shot': n, 'start': (n - 1) * 2., 'end': n * 2.,
             'source': {'shot_size': 'MS', 'action': 'Waits.'}}
            for n in range(1, 5)]


def test_wardrobe_change_splits_at_safe_source_cut():
    shots = source_shots()
    states = {1: {'lead': 'coat'}, 2: {}, 3: {'lead': 'dress'}, 4: {'lead': 'dress'}}
    clips = board.plan_dialogue_clips(shots, [], [], 20, character_states=states)
    assert [clip['shots'] for clip in clips] == [[1, 2], [3, 4]]


def test_scene_change_with_same_wardrobe_does_not_force_a_split():
    sequences = [{'first_shot': 1, 'last_shot': 2}, {'first_shot': 3, 'last_shot': 4}]
    states = {n: {'lead': 'coat'} for n in range(1, 5)}
    clips = board.plan_dialogue_clips(source_shots(), sequences, [], 20, character_states=states)
    assert [clip['shots'] for clip in clips] == [[1, 2, 3, 4]]


def test_sentence_crossing_wardrobe_change_is_not_cut_or_dropped():
    states = {1: {'lead': 'coat'}, 2: {'lead': 'coat'}, 3: {'lead': 'dress'}}
    with pytest.raises(ValueError, match='wardrobe change crosses a spoken sentence'):
        board.plan_dialogue_clips(source_shots(), [], [{'start': 3., 'end': 5.}], 20,
                                  character_states=states)


def test_explicit_state_refs_keep_sentence_across_costume_cut():
    shots = source_shots()
    states = {1: {'lead': 'coat'}, 2: {'lead': 'coat'}, 3: {'lead': 'dress'}, 4: {'lead': 'dress'}}
    lines = [{'start': 1., 'end': 5.}]
    clips = board.plan_dialogue_clips(shots, [], lines, 20, character_states=states,
                                      state_specific_references=True)
    assert [c['shots'] for c in clips] == [[1, 2, 3, 4]]
    assert shots == source_shots()


@pytest.mark.asyncio
async def test_one_pass_board_uses_prepared_character_states_to_plan_clips():
    states = {n: {'lead': 'coat' if n < 3 else 'dress'} for n in range(1, 5)}
    analysis = {'shots': source_shots(), 'video': {'duration': 8.},
                'source_verification': {'method': 'one_pass_production'}}
    cast = {'shots': {str(n): {'character_states': state} for n, state in states.items()}}
    result = await board.build_board(analysis, {}, cast=cast)
    assert [[shot['source_shot'] for shot in shots] for shots in result['shots'].values()] == [[1, 2], [3, 4]]
    assert [shot['duration_s'] for shots in result['shots'].values() for shot in shots] == [2.] * 4
