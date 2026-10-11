from copy import deepcopy
import json

import pytest

from flowboard.services import avis_text, source_generation_scope as scope
from flowboard.services.video_analyzer import one_pass_film as film, catalog_recovery
from tests.test_one_pass_voices import example


PREFIX = {'analysis':'full','source_start_seconds':0,'source_end_seconds':8.,
          'boundary_policy':'complete_clips_at_or_before_end'}


def test_prefix_preserves_full_evidence_and_never_crops_a_source_shot(tmp_path):
    analysis, _ = example(tmp_path)
    analysis['cuts'] = {'measurement_version':3, 'kept':[2.,4.,6.,8.,10.,12.,14.]}
    analysis['source_verification'] = {'status':'needs_review','findings':[{'shot':8,'message':'Held'}]}
    original = deepcopy(analysis)
    selected = scope.preparation_prefix(analysis,{**PREFIX,'source_end_seconds':7.})
    assert analysis == original
    assert [s['shot'] for s in selected['shots']] == [1,2,3]
    assert selected['video']['duration'] == 6.
    assert selected['cuts']['kept'] == [2.,4.]
    assert selected['transcript'] == original['transcript']
    assert selected['source_verification'] == original['source_verification']
    assert selected['production_scope']['full_source_video']['duration'] == 16.
    assert selected['production_scope']['source_fidelity_approved'] is False
    broken = deepcopy(original); broken['shots'].pop()
    with pytest.raises(ValueError,match='Full source reading is incomplete'):
        scope.preparation_prefix(broken,PREFIX)


@pytest.mark.asyncio
async def test_future_refusal_does_not_reprocess_or_approve_the_selected_prefix(tmp_path,monkeypatch):
    analysis, data = example(tmp_path)
    (tmp_path/'audio.wav').unlink()
    analysis['sequences'] = [{'first_shot':1,'last_shot':4},{'first_shot':5,'last_shot':8}]
    original = deepcopy(analysis)
    monkeypatch.setenv('FLOWBOARD_ONE_PASS_CATALOG_BATCH','4')
    calls = []
    async def call(payload,*args,**kwargs):
        ids = [s['shot'] for s in payload['shots']]; calls.append(ids)
        if ids[0]>=5: raise avis_text.AvisContentRefusal('declined later interval')
        out = deepcopy(data)
        out['shots'] = {k:v for k,v in out['shots'].items() if int(k) in ids}
        out['scenes'] = [{**row,'shot_ids':ids} for row in out['scenes']]
        return out
    monkeypatch.setattr(film,'_call',call)
    with pytest.raises(catalog_recovery.CatalogNeedsReview):
        await film.prepare(analysis,tmp_path,'Film',{})
    assert calls == [[1,2,3,4],[5,6,7,8]]
    full_report = (tmp_path/'one-pass-catalog-progress.json').read_bytes()
    full_cache = (tmp_path/'catalog-accepted-drafts.json').read_bytes()
    async def forbidden(*args,**kwargs):
        raise AssertionError('Retained prefix must not make new catalog calls')
    monkeypatch.setattr(film,'_read_catalog_batch',forbidden)
    prepared, cast, adaptation = await film.prepare(analysis,tmp_path,'Film',{},production_scope=PREFIX)
    assert [s['shot'] for s in prepared['shots']] == [1,2,3,4]
    assert set(adaptation['shots']) == {'1','2','3','4'}
    film.validate_prepared(prepared)
    assert prepared['source_verification']['status'] == 'observed'
    assert not prepared['source_verification']['independent_review']
    assert (tmp_path/'one-pass-catalog-progress.json').read_bytes() == full_report
    assert (tmp_path/'catalog-accepted-drafts.json').read_bytes() == full_cache
    assert analysis == original
    assert prepared['production_scope']['full_catalog_summary']['pending_shots'] == [5,6,7,8]
    again = await film.prepare(analysis,tmp_path,'Film',{},production_scope=PREFIX)
    assert again == (prepared,cast,adaptation)
    # An edited retained source contract invalidates a derived preparation.
    checkpoint = json.loads(full_cache)
    checkpoint['inventory']['shots']['1']['speakers'][0]['label'] = 'Changed source label'
    film.atomic(tmp_path/'catalog-accepted-drafts.json',checkpoint)
    # The full progress snapshot is also an accepted draft, so edit that source
    # snapshot consistently rather than testing two contradictory checkpoints.
    progress = json.loads(full_report)
    progress['inventory']['shots']['1']['speakers'][0]['label'] = 'Changed source label'
    film.atomic(tmp_path/'one-pass-catalog-progress.json',progress)
    changed, _, _ = await film.prepare(analysis,tmp_path,'Film',{},production_scope=PREFIX)
    assert changed['production_scope']['preparation_key'] != prepared['production_scope']['preparation_key']


@pytest.mark.asyncio
async def test_unresolved_selected_interval_stays_blocked(tmp_path,monkeypatch):
    analysis, _ = example(tmp_path)
    (tmp_path/'audio.wav').unlink()
    async def refuse(*args,**kwargs): raise avis_text.AvisContentRefusal('declined selected interval')
    monkeypatch.setattr(film,'_call',refuse)
    with pytest.raises(catalog_recovery.CatalogNeedsReview) as caught:
        await film.prepare(analysis,tmp_path,'Film',{},production_scope=PREFIX)
    assert caught.value.report['completed'] == 0
    assert caught.value.report['total'] == 4
    assert not list((tmp_path/'production-prefix').rglob('one-pass-production.json'))
    assert not (tmp_path/'one-pass-catalog-progress.json').exists()


@pytest.mark.asyncio
async def test_source_handoff_uses_derived_view_without_overwriting_full_reading(tmp_path,monkeypatch):
    import uuid
    from flowboard.db import get_session
    from flowboard.db.models import AutomationProject, VideoAnalysis
    from flowboard.services import source_film, production_run
    from flowboard.routes import video_analysis as routes
    from flowboard.services.video_analyzer import board as board_mod
    from tests.test_source_film import output
    analysis, data = example(tmp_path)
    (tmp_path/'audio.wav').unlink()
    async def catalog(payload,*a,**kw):
        ids = {s['shot'] for s in payload['shots']}
        out = deepcopy(data)
        out['shots'] = {k:v for k,v in out['shots'].items() if int(k) in ids}
        out['scenes'] = [{**r,'shot_ids':sorted(ids)} for r in out['scenes']]
        return out
    monkeypatch.setattr(film,'_call',catalog)
    rules = source_film.source_rules('anime_jp_modern')
    prepared, cast, adaptation = await film.prepare(analysis,tmp_path,'Film',rules,production_scope=PREFIX)
    for bucket in ('characters','environments','props','background_groups'):
        for entry in cast.get(bucket,[]): entry['design'] = {'ready':'retained design'}
    async def cached(*a,**kw):
        assert a[0] == analysis and kw['production_scope'] == PREFIX
        return deepcopy(prepared),deepcopy(cast),deepcopy(adaptation)
    monkeypatch.setattr(film,'prepare',cached)
    with get_session() as s:
        project = AutomationProject(name='Scoped production'); s.add(project); s.flush()
        row = VideoAnalysis(name='Full source',automation_project_id=project.id,status='analysed',
            analysis=deepcopy(analysis),options={'analysis_mode':'one_pass','auto_production':{
                'settings':source_film.FilmOptions(style='anime_jp_modern').model_dump(),
                'generation_scope':PREFIX}})
        s.add(row); s.commit(); vid=row.id
    async def board(source,adaptation,**kw):
        assert len(source['shots']) == 4
        assert source['video']['duration'] == 8.
        assert len(source_film.read(vid).analysis['shots']) == 8
        assert kw['cast']['characters'][0]['design'] == {'ready':'retained design'}
        return {**output(),'source_video_id':str(vid)}
    monkeypatch.setattr(board_mod,'build_board',board)
    monkeypatch.setattr(production_run,'start',lambda *a,**kw:{'id':str(uuid.uuid4())})
    await source_film.execute(vid)
    result = source_film.read(vid)
    assert result.analysis == analysis
    assert result.options['auto_production']['production_preparation']['full_source_shots'] == 8
    assert len(result.adaptation['shots']) == 4
