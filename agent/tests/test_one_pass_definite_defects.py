"""One-pass vision: a definite answer defect never stops a film or buys the same answer twice.

A missing descriptive field becomes "unclear"; a row-number slip is relabelled by position;
a kept answer is re-read under the current rules for free; a 429 waits and retries; a
completed but invalid answer gets one more request; an unknown outcome is never resent."""
import json

import pytest

from flowboard.services import avis_text
from flowboard.services.video_analyzer import one_pass
from flowboard.services.video_analyzer.frames import ShotSpan
from tests.test_one_pass_analysis import observation


def spans(tmp_path, numbers):
    from PIL import Image
    p = tmp_path / 'a.png'
    Image.new('RGB', (8, 8)).save(p)
    return [ShotSpan(n, n - 1, n, [p, p]) for n in numbers]


def test_a_missing_camera_detail_is_unclear_and_a_missing_action_still_fails(tmp_path):
    [span] = spans(tmp_path, [482])
    row = observation(482)
    del row['camera_elevation']
    out = one_pass.normalize([row], [span])[482]
    assert out['camera_elevation'] == 'unclear' and 'camera_elevation missing' in out['observation_warnings'][0]
    del row['action']
    with pytest.raises(ValueError, match='Missing action at shot 482'):
        one_pass.normalize([row], [span])


def test_a_slipped_row_number_is_relabelled_but_a_reordering_is_not(tmp_path):
    batch = spans(tmp_path, [645, 646, 647, 648])
    rows = [observation(n) for n in (645, 646, 647, 648)]
    rows[2]['shot'] = 646                          # shot 647's row labelled 646
    out = one_pass.normalize(rows, batch)
    assert sorted(out) == [645, 646, 647, 648] and 'renumbered' in out[647]['observation_warnings'][0]
    swapped = [observation(n) for n in (645, 646, 647, 648)]
    swapped[1]['shot'], swapped[2]['shot'] = 647, 646
    with pytest.raises(ValueError, match='reordered'):
        one_pass.normalize(swapped, batch)
    with pytest.raises(ValueError, match='omitted'):
        one_pass.normalize(rows[:3], batch)


@pytest.fixture
def calls(monkeypatch):
    log, answers = [], []

    async def complete(model, messages, **kw):
        log.append(kw)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return avis_text.Completion(text=answer, model=model)
    monkeypatch.setattr(avis_text, 'complete', complete)
    monkeypatch.setattr(one_pass, 'RATE_WAITS', (0, 0))
    return log, answers


@pytest.mark.asyncio
async def test_a_kept_answer_that_failed_old_rules_is_reread_for_free(tmp_path, calls):
    log, answers = calls
    [span] = spans(tmp_path, [1])
    row = observation(1)
    del row['composition']
    answers.append(json.dumps([row]))
    await one_pass.analyze([span], tmp_path)
    journal = next((tmp_path / 'one-pass-vision').glob('*.json'))
    data = json.loads(journal.read_text())
    # What the old code left behind for this answer: the raw text, marked unknown.
    data.update(state='failed_or_unknown', error='ValueError: Missing composition at shot 1')
    data.pop('output', None)
    journal.write_text(json.dumps(data))
    sources, _, meta = await one_pass.analyze([span], tmp_path)
    assert len(log) == 1 and meta['recovered_locally'] == 1 and sources[1]['composition'] == 'unclear'


@pytest.mark.asyncio
async def test_a_rate_limit_waits_and_retries_and_two_bad_answers_stop(tmp_path, calls):
    log, answers = calls
    [span] = spans(tmp_path, [1])
    answers += [avis_text.AvisRateLimited('busy'), json.dumps([observation(1)])]
    sources, _, _ = await one_pass.analyze([span], tmp_path / 'a')
    assert len(log) == 2 and sources[1]['action'] == 'looks back'
    answers += ['not json', json.dumps([{'shot': 1}])]
    with pytest.raises(RuntimeError, match='two definite answer defects'):
        await one_pass.analyze([span], tmp_path / 'b')
    assert len(log) == 4
    with pytest.raises(RuntimeError, match='two definite answer defects'):
        await one_pass.analyze([span], tmp_path / 'b')       # a resume buys nothing more
    assert len(log) == 4


@pytest.mark.asyncio
async def test_an_unknown_outcome_is_still_never_resent(tmp_path, calls):
    log, answers = calls
    [span] = spans(tmp_path, [1])
    answers.append(TimeoutError('gateway timeout'))
    with pytest.raises(RuntimeError, match='One-pass incomplete'):
        await one_pass.analyze([span], tmp_path)
    with pytest.raises(RuntimeError, match='no automatic paid retry'):
        await one_pass.analyze([span], tmp_path)
    assert len(log) == 1
