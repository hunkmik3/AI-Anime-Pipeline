"""Regression cases for missing speech, wrong speakers and over-fragmented scenes."""
from copy import deepcopy
import pytest
from flowboard.services import reskin_review_policy as policy, reskin_source_review as review
from flowboard.services.video_analyzer.board import plan_scene_dialogue_clips, plan_dialogue_clips
from tests.test_reskin_review_policy import dialogue_board, shots_of
from tests.test_reskin import observations


@pytest.mark.parametrize('expected,heard', [
    ('he married her','he carried her'), ('he killed him','he filled him'),
    ('one hundred dollars','one hundred collars'),
    ('lex norriston is here','lex norston is here'),
    ('i love you','no i love you'), ('i love you','i love you sweetheart'),
])
def test_changed_words_always_need_source_evidence(expected, heard):
    origin=policy.word_difference(policy.spoken(expected),policy.spoken(heard),edges=(True,True))
    assert policy.severity({'criterion':'speech_delivery','origin':origin}) == 'blocking'


@pytest.mark.parametrize('per_shot', [False,True])
def test_word_error_does_not_hide_wrong_speaker(per_shot):
    b,rows=dialogue_board()
    rows[0]['dialogue']=[{'who':'Hero','speaker_id':'hero','delivery':'on_camera','line':'Come here'}]
    rows[1]['dialogue']=[]
    answer=observations(shots_of(b,'c1'))
    answer['heard_dialogue']=[{'speaker':'Other','speaker_id':'crowd','delivery':'on_camera',
                               'text':'Oh come here','start':1.,'end':2.,
                               **({'shot_id':'c1:1'} if per_shot else {})}]
    found=review.validate(answer,rows,0)
    assert {'speech_interjection','speech_speaker'} <= {f['origin'] for f in found}


def test_inserted_word_does_not_shift_the_following_speaker():
    b,rows=dialogue_board()
    rows[0]['dialogue']=[{'who':'Hero','speaker_id':'hero','delivery':'on_camera','line':'Come here'}]
    rows[1]['dialogue']=[{'who':'Crowd','speaker_id':'crowd','delivery':'on_camera','line':'Wait now'}]
    answer=observations(shots_of(b,'c1'))
    answer['heard_dialogue']=[
        {'speaker':'Hero','speaker_id':'hero','delivery':'on_camera','text':'Oh come here','start':1.,'end':2.},
        {'speaker':'Crowd','speaker_id':'crowd','delivery':'on_camera','text':'Wait now','start':5.,'end':6.}]
    assert [f['origin'] for f in review.validate(answer,rows,0)] == ['speech_interjection']


def test_unknown_is_not_a_match_for_every_unnamed_voice():
    assert not review._same_speaker({'who':'Man on telephone'}, {'speaker_id':'unknown','speaker':'Unidentified woman'})


def shots(count, duration=1.):
    return [{'shot':i+1,'start':i*duration,'end':(i+1)*duration} for i in range(count)]


def lengths(source, clips):
    lookup={s['shot']:s for s in source}
    return [sum(lookup[n]['end']-lookup[n]['start'] for n in c['shots']) for c in clips]


def test_scene_plan_avoids_a_one_second_tail():
    source=shots(21)
    clips=plan_scene_dialogue_clips(source,[],[],20,min_clip_s=10,max_shots=12,hard_limit_s=20)
    assert sorted(lengths(source,clips)) == [10.,11.]
    assert [n for c in clips for n in c['shots']] == list(range(1,22))


def test_shot_count_is_soft_for_a_complete_sentence():
    source=shots(12)
    clips=plan_scene_dialogue_clips(source,[],[{'start':.2,'end':11.5}],20,min_clip_s=12,max_shots=4)
    assert len(clips)==1 and len(clips[0]['shots'])==12


def test_scene_change_is_preferred_over_one_long_cross_location_clip():
    source=shots(24)
    clips=plan_scene_dialogue_clips(source,[],[],30,min_clip_s=12,
                                  scene_ids={i:('room' if i<=12 else 'street') for i in range(1,25)})
    assert lengths(source,clips)==[12.,12.]


def test_short_real_scene_is_retained_without_padding_or_dropping():
    source=shots(23)
    clips=plan_scene_dialogue_clips(source,[],[],20,min_clip_s=10,
                                  scene_ids={i:('room' if i<=3 else 'street') for i in range(1,24)})
    assert lengths(source,clips)==[3.,20.]


def test_dialogue_crossing_cut_has_one_owner_and_all_shots_survive():
    source=shots(36)
    lines=[{'start':11.5,'end':21.5}]
    original=deepcopy(source)
    clips=plan_scene_dialogue_clips(source,[],lines,24,min_clip_s=12)
    assert [n for c in clips for n in c['shots']]==list(range(1,37))
    assert all(not(11.5<c['shots'][-1]<21.5) for c in clips)
    assert source==original and all(d<=24 for d in lengths(source,clips))


def test_unperformable_sentence_is_reported_not_truncated():
    with pytest.raises(ValueError,match='dialogue-safe'):
        plan_scene_dialogue_clips(shots(31),[],[{'start':0,'end':31}],30,min_clip_s=12)


def test_wardrobe_change_keeps_distinct_clips():
    source=shots(24)
    clips=plan_scene_dialogue_clips(source,[],[],30,min_clip_s=12,
                                  character_states={i:{'hero':'coat' if i<=12 else 'shirt'} for i in range(1,25)})
    assert lengths(source,clips)==[12.,12.]


def test_legacy_planner_remains_available_for_existing_projects():
    source=shots(12)
    clips=plan_dialogue_clips(source,[],[],15,max_shots=4,hard_limit_s=15)
    assert lengths(source,clips)==[4.,4.,4.]


def test_phone_intercuts_share_context_instead_of_creating_short_clips():
    source=shots(24)
    clips=plan_scene_dialogue_clips(source,[{'first_shot':1,'last_shot':24}],[],24,min_clip_s=12,
        scene_ids={i:('car' if i%2 else 'office') for i in range(1,25)},max_shots=12)
    assert all(n>=12 for n in lengths(source,clips))
    assert [n for c in clips for n in c['shots']]==list(range(1,25))


def test_flashback_under_one_voiceover_uses_separate_wardrobe_refs():
    source=shots(18)
    states={i:{'hero':'past' if 8<=i<=11 else 'present'} for i in range(1,19)}
    original=deepcopy(states)
    clips=plan_scene_dialogue_clips(source,[],[{'start':6.5,'end':12.5}],24,min_clip_s=12,
        character_states=states,state_specific_references=True)
    assert lengths(source,clips)==[18.]
    assert states==original
