import json
import uuid
from copy import deepcopy
import pytest
from sqlmodel import select
from flowboard.db import get_session
from flowboard.db.models import VideoAnalysis, AutomationProject, AutomationJob
from flowboard.routes import video_analysis as routes
from flowboard.services import source_film as film, film_styles, production_run


def source():
    return {
        "shots": [{"shot": 1, "source": {"action": "wait"}, "dialogue_lines": ["Hello."]}],
        "scene_inventory": {"assets": [{"id": "hero"}]},
        "source_verification": {
            "method": "source_frames",
            "status": "verified",
            "digest": "fixture",
            "reviewed_shots": [1],
            "unresolved_shots": [],
            "findings": [],
        },
    }


def output():
    return {
        "title": "Another film",
        "logline": "A new story",
        "runtime_seconds": 4,
        "source_video_id": "source-1",
        "characters": [
            {"key": "hero", "name": "Lina", "states": [{"key": "day", "wardrobe": "green coat"}]}
        ],
        "environments": [{"key": "room", "name": "Room"}],
        "assets": [],
        "sequences": [
            {
                "key": "clip-01",
                "label": "CLIP 01",
                "title": "Arrival",
                "duration_s": 4,
                "character_keys": ["hero"],
                "environment_key": "room",
            }
        ],
        "shots": {"clip-01": [{"source_shots": [1], "duration_s": 4}]},
    }


def test_upload_queues_new_project_and_settings_without_generation(client):
    with get_session() as s:
        original = AutomationProject(
            name="Do not replace", board={"style": "anime", "nodes": [{"id": "existing"}]}
        )
        s.add(original)
        s.commit()
        pid = original.id
    r = client.post(
        "/api/automation/videos",
        files={"file": ("new.mp4", b"fixture video", "video/mp4")},
        data={
            "project_id": str(pid),
            "auto_production": json.dumps(film.FilmOptions().model_dump()),
        },
    )
    assert r.status_code == 200, r.text
    vid = r.json()
    assert vid["automation_project_id"] != str(pid)
    with get_session() as s:
        assert s.get(AutomationProject, pid).board["nodes"][0]["id"] == "existing"
        jobs = s.exec(select(AutomationJob)).all()
        assert len(jobs) == 1 and jobs[0].kind == "source"
        assert jobs[0].payload["operation"]["task"] == "film"
        row = s.get(VideoAnalysis, uuid.UUID(vid["id"]))
        assert row.options["analysis_mode"] == "one_pass"
        assert row.options["auto_production"]["settings"]["resolution"] == "480p"


@pytest.mark.parametrize('mode', ['standard', 'fast'])
def test_upload_preserves_explicit_analysis_mode(client, mode):
    r = client.post('/api/automation/videos',
                    files={'file': ('source.mp4', b'fixture video', 'video/mp4')},
                    data={'analysis_mode': mode})
    assert r.status_code == 200, r.text
    with get_session() as s:
        row = s.get(VideoAnalysis, uuid.UUID(r.json()['id']))
        assert row.options['analysis_mode'] == mode


def test_invalid_auto_settings_create_nothing(client):
    r = client.post(
        "/api/automation/videos",
        files={"file": ("new.mp4", b"fixture", "video/mp4")},
        data={"auto_production": '{"style":"unknown"}'},
    )
    assert r.status_code == 422
    with get_session() as s:
        assert not s.exec(select(AutomationProject)).all()


def test_graph_settings_and_no_sample_cast():
    out = output()
    b = film.graph(out, film.FilmOptions().model_dump())
    assert b["style"] == film_styles.KEY and b["kyc"] and b["unmoderated"]
    assert b["aspectRatio"] == "1:1" and b["autoSourceFilm"] and b["dialogueLanguage"] == "source"
    assert any(n["id"] == "vid:clip-01" for n in b["nodes"])
    assert b["characters"][0]["name"] == "Lina" and "Theo" not in json.dumps(b)
    b["characters"][0]["name"] = "Changed"
    assert out["characters"][0]["name"] == "Lina"


def test_source_findings_and_translated_dialogue_block_without_acceptance():
    analysis = source()
    film.validate_source(analysis)
    adaptation = {"shots": {"1": {"dialogue": [{"who": "LINA", "line": "Hello."}]}}}
    film.validate_adaptation(analysis, adaptation)
    adaptation["shots"]["1"]["dialogue"][0]["line"] = "你好。"
    with pytest.raises(ValueError, match="dialogue differs"):
        film.validate_adaptation(analysis, adaptation)
    analysis["source_verification"]["findings"] = [{"shot": 1, "code": "uncertain prop"}]
    original = deepcopy(analysis)
    with pytest.raises(ValueError, match="verification needs attention"):
        film.validate_source(analysis)
    assert analysis == original


def test_source_cast_reuses_membership_retains_dependencies_and_skips_unused_candidates():
    analysis = source()
    analysis['scene_inventory'] = {
        'assets': [dict(id=key, kind=kind, name=key, description=key,
                        depends_on_asset_ids=deps, evidence_ids=[])
                   for key, kind, deps in [('hero','character',[]), ('room','environment',[]),
                                          ('photo','prop',['depicted']), ('depicted','character',[]),
                                          ('unused','character',[])]],
        'scenes': [],
        'shots': {'1': {'asset_presence': [dict(asset_id=key, visibility='visible', evidence_ids=[])
                                          for key in ('hero','room','photo')], 'evidence_ids': []}},
    }
    original = deepcopy(analysis)
    cast = film.cast_from_verified_inventory(analysis, 'A source film')
    assert {c['source_asset_id'] for c in cast['characters']} == {'hero','depicted'}
    assert cast['shots']['1']['character_keys'] == ['hero']
    assert cast['usage']['calls'] == 0 and cast['usage']['unused_candidates'] == ['unused']
    assert analysis == original


def test_empty_autosaved_script_scaffold_can_receive_source_board():
    project = AutomationProject(name='Source test', board={
        'autoSourceFilm': True, 'style': 'anime', 'edges': [],
        'nodes': [{'id':'script', 'type':'autoScript', 'data':{'kind':'script'},
                   'selected':True, 'measured':{'width':380}}],
    })
    assert film.empty_output_board(project)
    original = deepcopy(project.board)
    for data in ({'kind':'script', 'prompt':'Authored content'}, {'kind':'script', 'script':'Written story'}):
        project.board = {**original, 'nodes':[{**original['nodes'][0], 'data':data}]}
        assert not film.empty_output_board(project)
    project.board = original
    project.script = 'User script'
    assert not film.empty_output_board(project)
    project.script = ''
    project.board = {**original, 'autoSourceFilm':False}
    assert not film.empty_output_board(project)


@pytest.mark.asyncio
async def test_handoff_resumes_without_replacing_board_or_starting_second_run(monkeypatch):
    with get_session() as s:
        project = AutomationProject(name="auto")
        s.add(project)
        s.flush()
        row = VideoAnalysis(
            name="source",
            automation_project_id=project.id,
            status="analysed",
            analysis=source(),
            adaptation={"shots": {"1": {"dialogue": [{"who": "LINA", "line": "Hello."}]}}},
            cast={
                "characters": [{"key": "hero", "design": {"face": "Lina"}}],
                "environments": [{"key": "room", "design": {"walls": "pale"}}],
            },
            options={
                "auto_production": {
                    "settings": film.FilmOptions().model_dump(),
                    "stage": "analysis",
                }
            },
        )
        s.add(row)
        s.commit()
        vid = row.id
        pid = project.id

    async def adapted(*a, **k):
        pass

    async def board(*a, **k):
        return {**output(), "source_video_id": str(vid)}

    calls = []
    run_id = str(uuid.uuid4())

    def start(project_id, revision, config, key):
        calls.append((project_id, revision, config, key))
        return {"id": run_id}

    monkeypatch.setattr(routes, "_run_adaptation", adapted)
    monkeypatch.setattr(routes.board_mod, "build_board", board)
    monkeypatch.setattr(production_run, "start", start)
    await film.execute(vid)
    await film.execute(vid)
    assert len(calls) == 1 and calls[0][2]["mode"] == "render" and calls[0][2]["assemble"]
    with get_session() as s:
        p = s.get(AutomationProject, pid)
        r = s.get(VideoAnalysis, vid)
        assert p.revision == 1 and p.board["sourceVideoId"] == str(vid)
        assert r.options["auto_production"]["run_id"] == run_id
        assert r.options["auto_production"]["board_ready"]


@pytest.mark.asyncio
async def test_failed_verification_never_starts_production(monkeypatch):
    analysis = source()
    analysis["source_verification"]["unresolved_shots"] = [1]
    with get_session() as s:
        p = AutomationProject(name="auto")
        s.add(p)
        s.flush()
        r = VideoAnalysis(
            name="source",
            automation_project_id=p.id,
            status="analysed",
            analysis=analysis,
            options={"auto_production": {"settings": film.FilmOptions().model_dump()}},
        )
        s.add(r)
        s.commit()
        vid = r.id

    def forbidden(*a, **k):
        raise AssertionError("No production should be submitted")

    monkeypatch.setattr(production_run, "start", forbidden)
    with pytest.raises(ValueError, match="verification needs attention"):
        await film.execute(vid)
    r = film.read(vid)
    assert r.options["auto_production"]["stage"] == "blocked"
    assert r.analysis["source_verification"].get("review") is None


@pytest.mark.asyncio
@pytest.mark.parametrize('fixed', [False, True])
async def test_source_repair_is_bounded_and_must_really_resolve_findings(monkeypatch, fixed):
    analysis = source()
    analysis['scene_inventory']['shots'] = {'1': {'asset_presence': []}}
    analysis['source_verification'].update(status='needs_review', unresolved_shots=[1],
                                          findings=[{'shot':1,'code':'source_mismatch'}])
    with get_session() as session:
        project = AutomationProject(name='bounded source repair')
        session.add(project); session.flush()
        row = VideoAnalysis(name='source', automation_project_id=project.id, status='analysed',
            analysis=analysis, options={'auto_production':{'settings':film.FilmOptions().model_dump()}})
        session.add(row); session.commit(); key=row.id
    calls=[]
    async def repair(video_id):
        calls.append(video_id)
        if fixed:
            updated=deepcopy(analysis)
            updated['source_verification'].update(status='verified',findings=[],unresolved_shots=[])
            routes._update(video_id,analysis=updated,status='analysed')
    async def next_stage(*args, **kwargs):
        raise ValueError('reached adaptation after verified source')
    monkeypatch.setattr(routes,'_run_source_refinement',repair)
    monkeypatch.setattr(routes,'_run_adaptation',next_stage)
    for _ in range(2):
        with pytest.raises(ValueError,match='reached adaptation' if fixed else 'verification needs attention'):
            await film.execute(key)
    assert calls==[key]
    current=film.read(key)
    assert current.analysis['source_verification'].get('review') is None
    assert bool(current.analysis['source_verification']['findings']) is not fixed


@pytest.mark.asyncio
@pytest.mark.parametrize('review_masters', [False, True])
async def test_automatic_handoff_drives_materials_prompts_clips_and_final_assembly(monkeypatch, review_masters, client):
    from tests.test_production_run import fixture, drive, all_jobs

    board = fixture()
    for node in board["nodes"][:4]:
        node["data"]["identity"] = {}
        node["data"]["plate"] = {}
        node["data"]["states"] = {"day": {}}
    board.update(
        style=film_styles.KEY,
        autoSourceFilm=True,
        aspectRatio="1:1",
        dialogueLanguage="source",
        kyc=True,
        unmoderated=True,
    )
    analysis = source()
    with get_session() as s:
        p = AutomationProject(name="auto")
        s.add(p)
        s.flush()
        r = VideoAnalysis(
            name="source",
            automation_project_id=p.id,
            status="analysed",
            analysis=analysis,
            adaptation={"shots": {"1": {"dialogue": [{"who": "LINA", "line": "Hello."}]}}},
            cast={
                "characters": [{"key": "hero", "design": {"face": "Lina"}}],
                "environments": [{"key": "hall", "design": {"walls": "pale"}}],
            },
            options={"auto_production": {"settings": film.FilmOptions(review_masters=review_masters).model_dump()}},
        )
        s.add(r)
        s.commit()
        vid = r.id
        pid = p.id
    board["sourceVideoId"] = str(vid)

    async def adapted(*a, **k):
        pass

    async def build(*a, **k):
        return output()

    monkeypatch.setattr(routes, "_run_adaptation", adapted)
    monkeypatch.setattr(routes.board_mod, "build_board", build)
    monkeypatch.setattr(film, "graph", lambda *a: deepcopy(board))
    await film.execute(vid)
    rid = film.read(vid).options["auto_production"]["run_id"]
    if review_masters:
        from tests.test_production_run import get, finish
        assert get(rid).status == 'paused' and not all_jobs(pid)
        inventory = client.get(f'/api/automation/projects/{pid}/primary-materials').json()
        for item in inventory['items']:
            if not item['required']: continue
            resp = client.post(f'/api/automation/projects/{pid}/primary-materials/generate', json={
                'node_id': item['node_id'], 'expected_revision': inventory['revision'], 'request_key': item['node_id']})
            assert resp.status_code == 202, resp.text
            finish(get(resp.json()['id']))
        resp = client.post(f'/api/automation/projects/{pid}/primary-materials/continue', json={
            'run_id': rid, 'expected_revision': inventory['revision']})
        assert resp.status_code == 200, resp.text
    result = drive(pid, rid)
    assert result.status == "succeeded", result.error
    assert result.result["output"]["filename"] == "film.mp4"
    scheduled = all_jobs(pid)
    images = [j for j in scheduled if j.kind == "plate"]
    assert images and all(
        j.payload["style"] == film_styles.KEY and j.payload["aspect_ratio"] == "16:9"
        for j in images
    )
    assert all(j.payload["style_version"] == film_styles.version() for j in images)
    writes = [j for j in scheduled if j.kind == "write"]
    assert writes and all(
        j.payload["style"] == film_styles.KEY and j.payload["style_note"] == film_styles.VIDEO_STYLE
        for j in writes
    )
    for clip in [j for j in scheduled if j.kind == "clip"]:
        assert clip.payload["resolution"] == "480p" and clip.payload["aspect_ratio"] == "1:1"
        assert clip.payload["kyc_media_ids"] and clip.payload["unmoderated"]
    # Recovery after handoff does not enqueue a second paid run.
    count = len(scheduled)
    await film.execute(vid)
    assert len(all_jobs(pid)) == count


@pytest.mark.asyncio
async def test_real_board_builder_connects_all_source_assets_without_merging_cuts():
    from tests.test_production_inventory import film as source_fixture
    from flowboard.services.video_analyzer import board as builder
    from flowboard.services import shot_package
    from flowboard.routes.automation import ProductionRunConfig

    analysis = source_fixture("another-project")
    out = await builder.build_board(
        analysis,
        {"rules": film.source_rules(film_styles.KEY)},
        cast={"characters": [], "environments": []},
        preserve_source_shots=True,
        source_id="source-fresh",
    )
    b = film.graph(out, film.FilmOptions().model_dump())
    packs = shot_package.build(b, "new-project")
    report = production_run.preview(b, "new-project", ProductionRunConfig().model_dump())
    assert report["ready"], report["issues"]
    assert sum(len(p["shots"]) for p in packs) == 3
    assert {n["data"]["kind"] for n in b["nodes"]} >= {
        "script",
        "character",
        "environment",
        "asset",
        "sequence",
        "video",
    }
    assert b["sourceVideoId"] == "source-fresh"
    assert all(
        n["data"][n["data"]["kind"]]["key"].startswith("another-project")
        for n in b["nodes"]
        if n["data"]["kind"] in {"character", "environment", "asset"}
    )
    blockers = [
        i
        for p in packs
        for i in p["issues"]
        if i.get("blocking") and i["code"] not in {"missing_material", "missing_costume_sheet"}
    ]
    assert not blockers, blockers


@pytest.mark.asyncio
async def test_resume_preserves_source_review_and_reuses_committed_handoff(monkeypatch):
    analysis = source()
    with get_session() as s:
        p = AutomationProject(name="auto")
        s.add(p)
        s.flush()
        r = VideoAnalysis(
            name="source",
            automation_project_id=p.id,
            status="queued",
            analysis=analysis,
            options={
                "auto_production": {
                    "settings": film.FilmOptions().model_dump(),
                    "run_id": str(uuid.uuid4()),
                }
            },
        )
        s.add(r)
        s.commit()
        vid = r.id

    async def forbidden(*a, **k):
        raise AssertionError("Do not re-analyse a committed handoff")

    monkeypatch.setattr(routes, "_run_analysis", forbidden)
    await film.execute(vid)
    r = film.read(vid)
    assert r.status == "adapted" and r.analysis == analysis


def test_retry_invalidates_translated_cached_lines_without_accepting_or_changing_source():
    analysis = source()
    analysis["shots"].append({"shot": 2, "source": {"action": "wait"}, "dialogue_lines": ["Stay."]})
    with get_session() as s:
        row = VideoAnalysis(
            name="retry",
            analysis=analysis,
            adaptation={
                "shots": {
                    "1": {"dialogue": [{"who": "LINA", "line": "你好。"}]},
                    "2": {"dialogue": [{"who": "LINA", "line": "Stay."}]},
                }
            },
        )
        s.add(row)
        s.commit()
        vid = row.id
    film.invalidate_changed_dialogue(vid)
    row = film.read(vid)
    assert row.adaptation["stale_source_shots"] == [1] and row.analysis == analysis
