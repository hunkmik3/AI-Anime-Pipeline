"""Durable source-to-board handoff into the existing production controller.

Source jobs use the leased worker and analysis checkpoints. This stage creates
no images/videos itself, never accepts source findings, and never resubmits an
ambiguous provider request. Every upload owns a new project.
"""

from copy import deepcopy
from pydantic import BaseModel, Field, ConfigDict
from sqlmodel import select
from flowboard.db import get_session
from flowboard.db.models import AutomationProject, VideoAnalysis
from flowboard.services import film_styles, production_run


class FilmOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    style: str = Field(default=film_styles.KEY, pattern=film_styles.STYLE_PATTERN)
    aspect_ratio: str = Field(default="1:1", pattern="^(1:1|16:9|9:16)$")
    resolution: str = Field(default="480p", pattern="^(480p|720p|1080p)$")
    clip_seconds: float = Field(default=20, ge=5, le=30)
    timing_policy: str = Field(default='full_take', pattern='^(source_duration|full_take)$')
    casting_request: str = Field(default='', max_length=2000)
    review_masters: bool = True
    kyc: bool = True
    unmoderated: bool = True
    image_parallel: int = Field(default=4, ge=1, le=32)
    video_parallel: int = Field(default=4, ge=1, le=32)
    max_images: int = Field(default=500, ge=0, le=10000)
    max_videos: int = Field(default=500, ge=0, le=10000)


def source_rules(style):
    from flowboard.services.automation import _style

    keep = "Keep all source names unchanged; do not rename known people or terms. Use a stable descriptive ID for unnamed entities."
    return dict(
        visual_style=film_styles.rule_text(style)
        if film_styles.is_preset(style)
        else _style(style)["video_style"],
        character_names=keep,
        sect_names=keep,
        location_names=keep,
        technique_names=keep,
        dialogue_mode="verbatim",
        dialogue_language="the original language of each supplied line; no translation",
        preserve_editing=True,
    )


def graph(out, options):
    """Server equivalent of the board's node/edge format; no client needs to stay open."""
    opt = FilmOptions.model_validate(options)
    nodes = [
        {
            "id": "script",
            "type": "autoScript",
            "position": {"x": 0, "y": 0},
            "data": {"kind": "script"},
        }
    ]
    edges = []

    def edge(a, b):
        edges.append({"id": f"e-{a}-{b}", "source": a, "target": b})

    def plate(p=None):
        p = p or {}
        return {
            "prompt": p.get("prompt", ""),
            **({'referenceScope':deepcopy(p.get('referenceScope') or p.get('reference_scope'))}
               if p.get('referenceScope') or p.get('reference_scope') else {}),
            "status": "done" if p.get("reference_url") else "idle",
            **(
                {
                    "image": p.get("url"),
                    "referenceUrl": p["reference_url"],
                    "mediaId": p.get("media_id"),
                }
                if p.get("reference_url")
                else {}
            ),
        }

    for kind, bucket, prefix, x in [
        ("character", "characters", "char", 450),
        ("environment", "environments", "env", 1000),
        ("asset", "assets", "asset", 1550),
    ]:
        for i, item in enumerate(out.get(bucket) or []):
            data = {"kind": kind, kind: deepcopy(item)}
            if kind == "character":
                states = {st["key"]: plate() for st in item.get("states", [])}
                active = (item.get("plate") or {}).get("state_key") or next(iter(states), "")
                if (item.get("plate") or {}).get("state_key"):
                    states[active] = plate(item["plate"])
                data.update(identity=plate(item.get("plate")), states=states, activeState=active)
            else:
                data["plate"] = plate(item.get("plate"))
            nid = f"{prefix}:{item['key']}"
            nodes.append(
                {
                    "id": nid,
                    "type": {
                        "character": "autoCharacter",
                        "environment": "autoEnvironment",
                        "asset": "autoAsset",
                    }[kind],
                    "position": {"x": x, "y": i * 750},
                    "data": data,
                }
            )
            edge("script", nid)
    for i, seq in enumerate(out["sequences"]):
        key = seq["key"]
        sid = "seq:" + key
        vid = "vid:" + key
        nodes += [
            {
                "id": sid,
                "type": "autoSequence",
                "position": {"x": 2200, "y": i * 650},
                "data": {
                    "kind": "sequence",
                    "sequence": deepcopy(seq),
                    "shots": deepcopy(out["shots"][key]),
                    "cutStatus": "done",
                },
            },
            {
                "id": vid,
                "type": "autoVideo",
                "position": {"x": 2750, "y": i * 650},
                "data": {
                    "kind": "video",
                    "sequenceKey": key,
                    "label": seq["label"],
                    "title": seq["title"],
                    "durationS": seq["duration_s"],
                    "prompt": "",
                    "refs": [],
                    "status": "idle",
                },
            },
        ]
        edge("script", sid)
        edge(sid, vid)
        for char in seq.get("character_keys", []):
            edge("char:" + char, vid)
        for asset in seq.get("asset_keys", []):
            edge("asset:" + asset, vid)
        if seq.get("environment_key"):
            edge("env:" + seq["environment_key"], vid)
    return {
        "nodes": nodes,
        "edges": edges,
        "characters": deepcopy(out["characters"]),
        "environments": deepcopy(out["environments"]),
        "productionAssets": deepcopy(out.get("production_assets", [])),
        "sourceVerification": deepcopy(out.get("source_verification")),
        "autoSourceFilm": True,
        "sourcePipeline": "one_pass" if (out.get('source_verification') or {}).get('method') == 'one_pass_production' else 'verified_source',
        "dialogueLanguage": "source",
        "sourceVideoId": out["source_video_id"],
        "style": opt.style,
        "preserveSourceShots": True,
        "stateSpecificReferences": bool(out.get('state_specific_references')),
        "imageModel": "dola-seedream-5-0-pro",
        "imageSize": "2K",
        "aspectRatio": opt.aspect_ratio,
        "clipSeconds": opt.clip_seconds,
        "kyc": opt.kyc,
        "unmoderated": opt.unmoderated,
    }


def state(video_id, **changes):
    with get_session() as s:
        row = s.exec(
            select(VideoAnalysis).where(VideoAnalysis.id == video_id).with_for_update()
        ).one()
        current = dict(row.options['auto_production'])
        history = deepcopy(current.get('phase_history', {}))
        before, after = current.get('stage'), changes.get('stage')
        stamp = production_run.jobs.now().isoformat()
        if after and after != before:
            if before in history:
                history[before] = {**history[before], 'status': 'failed' if after == 'blocked' else 'succeeded', 'updated_at': stamp}
            if after not in ('blocked', 'production', 'queued'):
                history[after] = {'status': 'running', 'started_at': stamp, 'updated_at': stamp}
        row.options = {**row.options, 'auto_production': {**current, **changes, 'phase_history': history}}
        s.add(row)
        s.commit()


def read(video_id):
    with get_session() as s:
        row = s.get(VideoAnalysis, video_id)
        if not row:
            raise ValueError("Source video was deleted")
        return row


def validate_source(analysis):
    shots = analysis.get("shots") or []
    if not shots or any(not s.get("source") for s in shots):
        raise ValueError("Source shot analysis is incomplete; resume analysis before production.")
    if analysis.get("speech_error"):
        raise ValueError(
            "Source audio transcription failed; production cannot silently omit dialogue."
        )
    if (analysis.get('source_verification') or {}).get('method') == 'one_pass_production':
        from flowboard.services.video_analyzer.one_pass_film import validate_prepared
        validate_prepared(analysis)
    from flowboard.services.prompt_coverage import source_readiness_issues

    if not analysis.get("scene_inventory"):
        raise ValueError(
            "Source inventory is missing; cannot generate a film without its cast/props."
        )
    issues = source_readiness_issues(
        [{"source_shots": [s["shot"]]} for s in shots], analysis.get("source_verification")
    )
    if issues:
        raise ValueError("Source verification needs attention: " + "; ".join(issues[:8]))


def validate_adaptation(analysis, adaptation):
    from flowboard.services.video_analyzer.pipeline import stale_source_shots

    if stale_source_shots(adaptation):
        raise ValueError("Adapted shotlist is stale.")
    adapted = adaptation.get("shots") or {}
    for s in analysis["shots"]:
        shot = adapted.get(str(s["shot"]))
        if not shot:
            raise ValueError(f"Source shot {s['shot']} has no production shot.")
        expected = s.get("dialogue_lines") or []
        got = [d.get("line", "") for d in shot.get("dialogue", []) if d.get("line")]
        if expected != got:
            raise ValueError(
                f"Shot {s['shot']}: dialogue differs from source; verbatim text/language must be preserved."
            )


def cast_from_verified_inventory(analysis, title):
    """Reuse canonical source IDs and per-shot membership; no second cast guess.

    Unused extraction candidates stay in the source audit, but do not cause
    design/image work. Dependencies of used assets are retained transitively.
    """
    from flowboard.services.video_analyzer import production
    validate_source(analysis)
    cast = production.attach_inventory(analysis, {'title': title, 'logline': '', 'shots': {}})
    inventory = analysis['scene_inventory']
    used = {p['asset_id'] for row in inventory.get('shots', {}).values()
            for p in row.get('asset_presence', []) if p.get('visibility') != 'absent'}
    for row in inventory.get('shots', {}).values():
        for p in row.get('asset_presence', []):
            if p.get('visibility') == 'absent':
                continue
            used.update(p.get('contains_ids') or [])
            if p.get('holder_id'):
                used.add(p['holder_id'])
    aliases = cast.get('source_identity_aliases') or {}
    used = {aliases.get(key, key) for key in used}
    while True:
        more = {aliases.get(dep, dep) for a in inventory.get('assets', []) if a['id'] in used
                for dep in (a.get('depends_on_asset_ids') or []) + (a.get('member_ids') or [])}
        if more <= used:
            break
        used.update(more)
    for bucket in production.BUCKETS.values():
        cast[bucket] = [entry for entry in cast.get(bucket, []) if entry['source_asset_id'] in used]
    cast['assets'] = cast.get('props', []) + cast.get('background_groups', [])
    cast['usage'] = {'calls': 0, 'method': 'verified_source_inventory', 'unused_candidates':
                     sorted(a['id'] for a in inventory.get('assets', []) if a['id'] not in used)}
    return cast


def empty_output_board(project):
    """The UI may autosave its blank script node before source work finishes."""
    if project.script or project.title or project.logline:
        return False
    board = project.board or {}
    if any(board.get(key) for key in ('edges', 'characters', 'environments', 'productionAssets', 'sourceVideoId')):
        return False
    nodes = board.get('nodes') or []
    if not nodes:
        return True
    return (board.get('autoSourceFilm') is True and len(nodes) == 1
            and nodes[0].get('id') == 'script' and nodes[0].get('type') == 'autoScript'
            and nodes[0].get('data') == {'kind': 'script'})


def invalidate_changed_dialogue(video_id):
    """On retry, regenerate only cached shots which changed the source dialogue."""
    with get_session() as s:
        row = s.exec(
            select(VideoAnalysis).where(VideoAnalysis.id == video_id).with_for_update()
        ).one()
        adaptation = deepcopy(row.adaptation or {})
        changed = set(adaptation.get("stale_source_shots") or [])
        for source in row.analysis.get("shots", []):
            cached = (adaptation.get("shots") or {}).get(str(source["shot"]))
            if cached and (source.get("dialogue_lines") or []) != [
                d.get("line", "") for d in cached.get("dialogue", []) if d.get("line")
            ]:
                changed.add(source["shot"])
        if changed:
            adaptation["stale_source_shots"] = sorted(changed)
            row.adaptation = adaptation
            s.add(row)
            s.commit()


async def execute(video_id):
    from flowboard.routes import video_analysis as routes
    from flowboard.routes.automation import ProductionRunConfig
    from flowboard.services.video_analyzer import board as board_mod

    row = read(video_id)
    cfg = row.options.get("auto_production") or {}
    opt = FilmOptions.model_validate(cfg["settings"])
    one_pass = row.options.get('analysis_mode') == 'one_pass'
    try:
        # A committed handoff is never submitted again on worker recovery.
        if cfg.get("run_id"):
            routes._update(video_id, status="adapted", error=None)
            return
        state(video_id, stage="analysis", error=None)
        if (
            not row.analysis.get("shots")
            or (not one_pass and not row.analysis.get("scene_inventory"))
            or any(not shot.get("source") for shot in row.analysis.get("shots", []))
        ):
            await routes._run_analysis(video_id, None)
            row = read(video_id)
            if row.status == "failed":
                raise ValueError(row.error)
        # A queued resume must retain edits and accepted review on the DB source,
        # rather than replace them with an older disk analysis checkpoint.
        report = row.analysis.get('source_verification') or {}
        current_cfg = row.options.get('auto_production') or {}
        if (not one_pass and row.analysis.get('scene_inventory', {}).get('shots')
                and (report.get('status') != 'verified' or report.get('findings') or report.get('unresolved_shots'))
                and not row.analysis.get('source_refinement_history')
                and not row.analysis.get('source_refinement_attempted')
                and not current_cfg.get('automatic_source_refinement_attempted')):
            # One bounded engine repair before giving up; never accept findings
            # and never repeat the paid repair on worker recovery/resume.
            state(video_id, stage='source_refinement', automatic_source_refinement_attempted=True)
            await routes._run_source_refinement(video_id)
            row = read(video_id)
            if row.status == 'failed':
                raise ValueError(row.error)
        rules = source_rules(opt.style)
        if one_pass:
            from flowboard.services.video_analyzer import one_pass_film
            state(video_id, stage='profiles')
            if (row.analysis.get('source_verification') or {}).get('method') != one_pass_film.METHOD:
                prepared, cast, adaptation = await one_pass_film.prepare(
                    row.analysis, routes._work_dir(video_id), row.name, rules,
                    on_progress=routes._progress_writer(video_id))
                routes._update(video_id, analysis=prepared, cast=cast, adaptation=adaptation)
                row = read(video_id)
            elif row.adaptation.get('rules') != rules:
                raise ValueError('Style/settings changed after preparation; create a new film rather than mixing cached designs.')
        validate_source(row.analysis)
        state(video_id, stage="adaptation")
        if not one_pass:
            invalidate_changed_dialogue(video_id)
            await routes._run_adaptation(video_id, rules, None)
        row = read(video_id)
        if row.status == "failed":
            raise ValueError(row.error)
        validate_adaptation(row.analysis, row.adaptation)
        if not one_pass and (not row.cast.get("characters") or not row.cast.get("environments")):
            state(video_id, stage="profiles")
            if row.analysis.get('scene_inventory', {}).get('shots'):
                routes._update(video_id, cast=cast_from_verified_inventory(row.analysis, row.name))
            else:
                await routes._run_cast(video_id)
            row = read(video_id)
            if row.status == "failed":
                raise ValueError(row.error)
        entries = [
            e
            for bucket in ("characters", "environments", "props", "background_groups")
            for e in row.cast.get(bucket, [])
        ]
        if opt.casting_request.strip():
            from flowboard.services import target_casting
            state(video_id, stage='casting')
            receipt=row.adaptation.get('target_casting')
            if receipt and receipt['request']!=opt.casting_request:
                raise ValueError('Casting request changed after preparation; create a new film.')
            if not receipt:
                receipt=await target_casting.resolve(opt.casting_request,row.cast,routes._work_dir(video_id))
                routes._update(video_id,cast=target_casting.apply_cast(row.cast,receipt),
                               adaptation={**row.adaptation,'target_casting':receipt})
                row=read(video_id)
                entries=[e for bucket in ('characters','environments','props','background_groups') for e in row.cast.get(bucket,[])]
        pending = [e["key"] for e in entries if not e.get("design") or e.get("design_error")]
        if pending:
            state(video_id, stage="design")
            await routes._run_design(video_id, pending, concurrency=8)
            row = read(video_id)
            if row.status == "failed":
                raise ValueError(row.error)
            errors = [
                e.get("name", e.get("key"))
                for bucket in ("characters", "environments", "props", "background_groups")
                for e in row.cast.get(bucket, [])
                if e.get("design_error")
            ]
            if errors:
                raise ValueError("Incomplete design profiles: " + ", ".join(errors))
        state(video_id, stage="board")
        with get_session() as s:
            existing = s.get(AutomationProject, row.automation_project_id)
        if not existing:
            raise ValueError("Output project was deleted")
        if empty_output_board(existing):
            out = await board_mod.build_board(
                row.analysis,
                row.adaptation,
                cast=row.cast,
                max_clip_s=opt.clip_seconds,
                preserve_source_shots=True,
                source_id=str(video_id),
            )
            board = graph(out, opt.model_dump())
            if row.adaptation.get('target_casting'):
                from flowboard.services import target_casting
                board=target_casting.apply_board(board,row.adaptation['target_casting'])
            with get_session() as s:
                project = s.exec(
                    select(AutomationProject)
                    .where(AutomationProject.id == row.automation_project_id)
                    .with_for_update()
                ).one()
                if not empty_output_board(project):
                    raise ValueError(
                        "Output board was edited while preparing the film; it was not overwritten."
                    )
                project.board = board
                project.title = out["title"]
                project.logline = out["logline"]
                project.runtime_seconds = out["runtime_seconds"]
                project.revision += 1
                s.add(project)
                s.commit()
                revision = project.revision
        else:
            if existing.board.get("sourceVideoId") != str(video_id):
                raise ValueError("Output board belongs to different source inputs.")
            revision = existing.revision
        state(video_id, board_ready=True, stage="production")
        config = ProductionRunConfig(
            mode="render",
            review_masters=opt.review_masters,
            assemble=True,
            resolution=opt.resolution,
            timing_policy=opt.timing_policy,
            image_parallel=opt.image_parallel,
            video_parallel=opt.video_parallel,
            max_images=opt.max_images,
            max_videos=opt.max_videos,
            max_text=10000,
        ).model_dump()
        run = production_run.start(
            row.automation_project_id, revision, config, "source-film:" + str(video_id)
        )
        state(video_id, run_id=run["id"], stage="production", error=None)
    except Exception as exc:
        state(video_id, stage="blocked", error=str(exc)[:1200])
        routes._update(video_id, status="failed", error=str(exc)[:1000])
        raise
