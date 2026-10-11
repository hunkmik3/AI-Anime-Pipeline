"""Language probing cost: one encoder pass per 28 s block on a monolingual film; the old
5 s probes only where a block is uncertain, and 1 s probes around a real transition."""
from flowboard.services.video_analyzer import asr_multilingual_worker as w


def prober(language_at, uncertain=()):
    calls = []

    def probe(center, width):
        calls.append((round(center, 2), round(width, 2)))
        mixed = any(a <= center < b for a, b in uncertain) and width > 5
        return {'center': center, 'start': center - width / 2, 'end': center + width / 2,
                'language': language_at(center), 'probability': .55 if mixed else .97}
    return probe, calls


def test_a_monolingual_film_costs_one_probe_per_block():
    probe, calls = prober(lambda t: 'en')
    probes = w.detect_runs(probe, 1859.)
    assert len(calls) == 67 and all(width == 28 or width < 28 for _, width in calls)     # was 372 at 5 s
    assert w.language_runs(probes, 1859.) == [{'start': 0., 'end': 1859., 'language': 'en'}]


def test_an_uncertain_block_is_reprobed_finely_and_its_switch_is_found():
    # A Spanish aside from 70 s to 80 s inside block 56-84: that block's own probe is uncertain.
    probe, calls = prober(lambda t: 'es' if 70 <= t < 80 else 'en', uncertain=[(56, 84)])
    probes = w.detect_runs(probe, 168.)
    runs = w.language_runs(probes, 168.)
    assert [r['language'] for r in runs] == ['en', 'es', 'en']
    assert 68 <= runs[1]['start'] <= 72 and 78 <= runs[1]['end'] <= 82
    fine = [c for c, width in calls if 2 < width <= 5]
    assert len(fine) == 6 and all(56 <= c < 84 for c in fine)                            # only inside 56-84
    assert not any(p['probability'] < w.CONFIDENT and p['end'] - p['start'] > 5 for p in probes)


def test_silent_blocks_are_skipped():
    probe_calls = []

    def probe(center, width):
        probe_calls.append(center)
        return None
    assert w.detect_runs(probe, 100.) == [] and len(probe_calls) == 4
    assert w.language_runs([], 100.) == [{'start': 0., 'end': 100., 'language': None}]
