import copy

from flowboard.services.video_analyzer.source_narrative_context import build_narrative_context


def test_narration_is_exact_timed_context_not_audio_verification_or_presence():
    analysis = {'transcript': {'segments': [
        {'start': 0, 'end': 2, 'text': 'Earlier unrelated scene.'},
        {'start': 50, 'end': 53, 'text': 'The driver removed his mask.'},
        {'start': 56, 'end': 59, 'text': 'It was her husband.'},
        {'start': 200, 'end': 201, 'text': 'Distant unrelated scene.'}]}}
    before = copy.deepcopy(analysis)
    context = build_narrative_context(analysis, [{'start': 55, 'end': 60}])
    assert [r['id'] for r in context['segments']] == ['asr-segment-1', 'asr-segment-2']
    assert context['segments'][1] == {'id': 'asr-segment-2', 'start': 56, 'end': 59, 'text': 'It was her husband.'}
    assert context['audio_independently_verified'] is False
    assert context['scope'] == 'story_context_only_not_visual_presence_proof'
    assert analysis == before


def test_invalid_and_untimed_transcripts_cannot_become_context():
    rows = [{'start': float('nan'), 'end': 3, 'text': 'bad'},
            {'start': True, 'end': 4, 'text': 'bad'}, {'text': 'untimed'},
            {'start': 4, 'end': 3, 'text': 'backwards'}]
    assert not build_narrative_context({'transcript': rows}, [{'start': 0, 'end': 5}])['segments']
    assert not build_narrative_context({}, [{'start': 0, 'end': 5}])['segments']
