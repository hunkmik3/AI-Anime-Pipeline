"""Tests for prompt_synth service + /api/prompt/* routes (Phase 6 anime).

Coverage focuses on STRUCTURAL guarantees, not exact prompt wording:
  - Multi-subject detection (character + image-sibling cases)
  - Prompt-vs-aiBrief precedence
  - Bible auto-injection (Project + Scene)
  - Master-shot `establishing_shot_ref` slot ordering
  - Script / BibleRef passthrough node text surfacing
  - Bilingual mock (VN input → English-output instruction present)
  - Script parse endpoint shape

Provider routing is bypassed by patching ``run_llm`` at the import
boundary in ``prompt_synth``. Real provider tests live in
``test_llm_registry.py``.

Fashion-era prompts are preserved verbatim in
``services/prompt_synth_legacy.py`` for reference; tests for the
fashion behaviour were removed when the anime rewrite landed
(Phase 6).
"""
from __future__ import annotations

import json as _json
import uuid

import pytest

from flowboard.db import get_session
from flowboard.db.models import Asset, Edge, Node, Project, Scene, Shot
from flowboard.services import prompt_synth


# ── Shot pyramid helpers ────────────────────────────────────────────────


def _make_shot(
    session,
    *,
    name: str = "t",
    project_bible: dict | None = None,
    master_establishing_asset_id: int | None = None,
) -> Shot:
    """Build Project → Scene → Shot and return the Shot.

    Bible fields are optional so tests that don't care about
    injection still get clean defaults (empty project bible → no prepend
    block). Phase 8.3: Scene Bible removed.
    """
    project = Project(name=name, project_bible=project_bible or {})
    session.add(project)
    session.flush()
    scene = Scene(
        project_id=project.id,
        name="Scene 1",
        order_index=0,
        master_establishing_asset_id=master_establishing_asset_id,
    )
    session.add(scene)
    session.flush()
    shot = Shot(scene_id=scene.id, order_index=0)
    session.add(shot)
    session.commit()
    session.refresh(shot)
    return shot


def _seed_simple_char_chain() -> dict:
    """Project → Scene → Shot with character → image target chain.

    Returns ids of the target image + the character. No bible.
    """
    with get_session() as s:
        b = _make_shot(s, name="basic")
        char = Node(
            shot_id=b.id,
            short_id="char",
            type="character",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Hero",
                "aiBrief": "young woman, short black hair, twin braids, cel-shaded",
                "mediaId": "uuuuuuuu-1111-2222-3333-444444444444",
            },
            status="done",
        )
        target = Node(
            shot_id=b.id,
            short_id="targ",
            type="image",
            x=0, y=0, w=240, h=180,
            data={"title": "Cinematic shot"},
            status="idle",
        )
        s.add_all([char, target])
        s.commit()
        s.refresh(char); s.refresh(target)
        s.add(Edge(shot_id=b.id, source_id=char.id, target_id=target.id))
        s.commit()
        return {"target_id": target.id, "char_id": char.id, "shot_id": b.id}


# ── Anime style floor (system prompt structure) ─────────────────────────


@pytest.mark.asyncio
async def test_anime_image_system_prompt_carries_style_floor(client, monkeypatch):
    """System prompt for an image target must declare the anime style
    floor: cel-shaded, 2D, anti-photoreal, anti-3D, anti-text overlays,
    English output, bilingual VN input.
    """
    ids = _seed_simple_char_chain()
    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["system_prompt"] = system_prompt or ""
        captured["prompt"] = prompt
        return "Wide establishing cel anime shot."

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(ids["target_id"])

    sp = captured["system_prompt"]
    assert "cel-shaded" in sp.lower()
    assert "2D anime" in sp or "2d anime" in sp.lower()
    # Anti-patterns required so providers don't drift toward photoreal.
    assert "No photoreal" in sp or "no photoreal" in sp.lower()
    assert "no 3D" in sp.lower() or "not 3d-rendered" in sp.lower()
    assert "no text overlays" in sp.lower()
    # Camera vocabulary explicit so the LLM picks an angle.
    assert "wide establishing" in sp.lower()
    assert "over-the-shoulder" in sp.lower()
    # Bilingual hand-off: source may be VN, output EN.
    assert "vietnamese" in sp.lower()
    assert "english" in sp.lower()
    # Single-subject base — no multi-subject clause.
    assert "MULTI-SUBJECT MODE" not in sp


@pytest.mark.asyncio
async def test_anime_video_system_prompt_anime_cadence_and_anti_freeze(
    client, monkeypatch
):
    """Video target → motion prompt with anime cadence rule (twos /
    threes / sakuga ones), anti-freeze safety floor, camera vocabulary,
    silent-by-default audio, bilingual EN output."""
    with get_session() as s:
        b = _make_shot(s, name="vid")
        src = Node(
            shot_id=b.id, short_id="src", type="image",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Source",
                "aiBrief": "young woman standing in alley at night, cel-shaded",
                "mediaId": "uuuuuuuu-vid1-1111-2222-333333333333",
            },
            status="done",
        )
        vid = Node(
            shot_id=b.id, short_id="vid", type="video",
            x=0, y=0, w=240, h=180,
            data={"title": "Clip"},
            status="idle",
        )
        s.add_all([src, vid]); s.commit(); s.refresh(src); s.refresh(vid)
        s.add(Edge(shot_id=b.id, source_id=src.id, target_id=vid.id))
        s.commit()
        vid_id = vid.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["system_prompt"] = system_prompt or ""
        return "slow push-in, rain catches the streetlight"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(vid_id)

    sp = captured["system_prompt"].lower()
    # Anime cadence rule with modern sakuga allowance.
    assert "twos or threes" in sp or "on twos" in sp or "animate on twos" in sp
    assert "sakuga" in sp
    assert "ones" in sp
    # Anti-freeze safety floor.
    assert "anti-freeze" in sp
    # Anime camera vocabulary.
    assert "push-in" in sp
    assert "pan" in sp
    # Audio: silent by default (no synthetic dialogue).
    assert "silent by default" in sp
    # Bilingual: VN input ok, EN output.
    assert "english" in sp


@pytest.mark.asyncio
async def test_anime_video_static_camera_locks(client, monkeypatch):
    """When camera='static' the synth appends a lock clause that forbids
    push-in / pull-out / pan / zoom / handheld."""
    with get_session() as s:
        b = _make_shot(s, name="stat")
        src = Node(
            shot_id=b.id, short_id="src2", type="image",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Source",
                "aiBrief": "school classroom, golden hour",
                "mediaId": "uuuuuuuu-stat-1111-2222-333333333333",
            },
            status="done",
        )
        vid = Node(
            shot_id=b.id, short_id="vid2", type="video",
            x=0, y=0, w=240, h=180,
            data={"title": "Vid"},
            status="idle",
        )
        s.add_all([src, vid]); s.commit(); s.refresh(src); s.refresh(vid)
        s.add(Edge(shot_id=b.id, source_id=src.id, target_id=vid.id))
        s.commit()
        vid_id = vid.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["system_prompt"] = system_prompt or ""
        return "blink, breath rise, dust drifting in beam"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(vid_id, camera="static")

    sp = captured["system_prompt"].lower()
    assert "static" in sp
    assert "locked-off" in sp
    # The lock must forbid pan/zoom variants
    assert "no push-in" in sp or "no pan" in sp


# ── Bible auto-injection ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bible_injection_project_bible_prepends(client, monkeypatch):
    """When Project Bible has style fields, they prepend the user message
    as a labeled PROJECT BIBLE block.
    """
    with get_session() as s:
        b = _make_shot(
            s,
            name="bible-proj",
            project_bible={
                "art_style": "warm noir office drama, painterly backgrounds",
                "color_palette": ["amber", "navy", "ember red"],
                "line_style": "thin uneven ink line",
                "lighting_conventions": "low-key, single warm key from desk lamp",
                "negative_prompts": ["3D", "photoreal", "western cartoon"],
            },
        )
        char = Node(
            shot_id=b.id, short_id="bch", type="character",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Detective",
                "aiBrief": "man in his 40s, rumpled coat, tired eyes",
                "mediaId": "uuuuuuuu-bch1-1111-2222-333333333333",
            },
            status="done",
        )
        tgt = Node(
            shot_id=b.id, short_id="btg", type="image",
            x=0, y=0, w=240, h=180,
            data={"title": "Cold open"},
            status="idle",
        )
        s.add_all([char, tgt]); s.commit()
        for n in (char, tgt):
            s.refresh(n)
        s.add(Edge(shot_id=b.id, source_id=char.id, target_id=tgt.id))
        s.commit()
        tgt_id = tgt.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        return "Warm-noir close-up of the detective."

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(tgt_id)

    user = captured["prompt"]
    assert "PROJECT BIBLE" in user
    assert "warm noir office drama" in user
    assert "amber, navy, ember red" in user
    assert "thin uneven ink line" in user
    assert "low-key" in user
    assert "3D, photoreal, western cartoon" in user
    # Bible block prepends (sits above upstream nodes section).
    bible_pos = user.find("PROJECT BIBLE")
    subj_pos = user.find("Subject(s)")
    assert bible_pos != -1 and subj_pos != -1
    assert bible_pos < subj_pos


# Phase 8.3: Scene Bible removed — the scene-bible-prepends test was deleted.
# Project Bible injection (above) is unchanged + still covered.


@pytest.mark.asyncio
async def test_bible_injection_empty_bible_skips_block(client, monkeypatch):
    """A project/scene with no bible filled in must NOT inject a noisy
    empty block — the user message goes straight to upstream context."""
    ids = _seed_simple_char_chain()
    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(ids["target_id"])

    user = captured["prompt"]
    assert "PROJECT BIBLE" not in user
    assert "SCENE BIBLE" not in user


@pytest.mark.asyncio
async def test_bible_injection_partial_fields_only_present_ones(
    client, monkeypatch
):
    """When only some bible fields are filled, only those lines render —
    no empty placeholder rows."""
    with get_session() as s:
        b = _make_shot(
            s,
            name="bible-partial",
            project_bible={
                "art_style": "soft watercolor",
                # palette, line, lighting, negative intentionally empty
            },
        )
        char = Node(
            shot_id=b.id, short_id="par", type="character",
            x=0, y=0, w=240, h=180,
            data={"title": "x", "aiBrief": "girl in field",
                  "mediaId": "uuuuuuuu-par1-1111-2222-333333333333"},
            status="done",
        )
        tgt = Node(
            shot_id=b.id, short_id="ptg", type="image",
            x=0, y=0, w=240, h=180, data={"title": "x"},
            status="idle",
        )
        s.add_all([char, tgt]); s.commit()
        for n in (char, tgt):
            s.refresh(n)
        s.add(Edge(shot_id=b.id, source_id=char.id, target_id=tgt.id))
        s.commit()
        tgt_id = tgt.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(tgt_id)
    user = captured["prompt"]
    assert "Art style: soft watercolor" in user
    assert "Palette:" not in user
    assert "Line style:" not in user
    assert "Lighting:" not in user
    assert "Negative:" not in user


# ── Master shot establishing_shot_ref ───────────────────────────────────


@pytest.mark.asyncio
async def test_master_shot_takes_establishing_shot_ref_label(
    client, monkeypatch
):
    """A ``master_shot`` upstream node with a media binding earns the
    ``establishing_shot_ref`` label, not a numbered ``ref_image_N``.
    Other ref-source siblings start numbering at 1."""
    with get_session() as s:
        b = _make_shot(s, name="master")
        master = Node(
            shot_id=b.id, short_id="mst", type="master_shot",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Scene master",
                "aiBrief": "rooftop establishing wide, dusk neon, hero centered",
                "mediaId": "uuuuuuuu-mst1-1111-2222-333333333333",
            },
            status="done",
        )
        char = Node(
            shot_id=b.id, short_id="mch", type="character",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Hero",
                "aiBrief": "young Vietnamese woman, twin braids",
                "mediaId": "uuuuuuuu-mch1-1111-2222-333333333333",
            },
            status="done",
        )
        tgt = Node(
            shot_id=b.id, short_id="mtg", type="image",
            x=0, y=0, w=240, h=180, data={"title": "Hero closeup"},
            status="idle",
        )
        s.add_all([master, char, tgt]); s.commit()
        for n in (master, char, tgt):
            s.refresh(n)
        s.add(Edge(shot_id=b.id, source_id=master.id, target_id=tgt.id))
        s.add(Edge(shot_id=b.id, source_id=char.id, target_id=tgt.id))
        s.commit()
        tgt_id = tgt.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        captured["system_prompt"] = system_prompt or ""
        return "Close-up matched to establishing_shot_ref lighting."

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(tgt_id)

    user = captured["prompt"]
    sp = captured["system_prompt"]
    # Master shot earns the dedicated label.
    assert "establishing_shot_ref:" in user
    # Character ref starts at ref_image_1 (not bumped by master shot).
    assert "ref_image_1:" in user
    # The user message surfaces the master under its own labeled section.
    assert "Establishing shot" in user
    # System prompt knows about the establishing reference.
    assert "establishing_shot_ref" in sp


@pytest.mark.asyncio
async def test_master_shot_without_media_does_not_get_label(client, monkeypatch):
    """A bare master_shot node (no mediaId / no masterShotAssetId) has
    nothing to bind — no establishing_shot_ref label is assigned."""
    with get_session() as s:
        b = _make_shot(s, name="master-empty")
        master = Node(
            shot_id=b.id, short_id="mbe", type="master_shot",
            x=0, y=0, w=240, h=180,
            data={"title": "Master (unset)"},
            status="idle",
        )
        tgt = Node(
            shot_id=b.id, short_id="mbt", type="image",
            x=0, y=0, w=240, h=180, data={"title": "shot"},
            status="idle",
        )
        s.add_all([master, tgt]); s.commit()
        for n in (master, tgt):
            s.refresh(n)
        s.add(Edge(shot_id=b.id, source_id=master.id, target_id=tgt.id))
        s.commit()
        tgt_id = tgt.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(tgt_id)
    assert "establishing_shot_ref:" not in captured["prompt"]


@pytest.mark.asyncio
async def test_master_shot_resolves_media_id_via_asset_id(client, monkeypatch):
    """When MasterShotNode stored only ``masterShotAssetId`` (no
    ``mediaId``), the backend looks up Asset.uuid_media_id and still
    assigns the establishing_shot_ref label."""
    with get_session() as s:
        proj = Project(name="master-asset-lookup")
        s.add(proj); s.flush()
        asset = Asset(
            project_id=proj.id,
            kind="image",
            uuid_media_id="uuuuuuuu-aaaa-1111-2222-333333333333",
            local_path="/tmp/x.png",
        )
        s.add(asset); s.commit(); s.refresh(asset)
        scene = Scene(project_id=proj.id, name="Scene", order_index=0)
        s.add(scene); s.flush()
        shot = Shot(scene_id=scene.id, order_index=0)
        s.add(shot); s.commit(); s.refresh(shot)
        master = Node(
            shot_id=shot.id, short_id="mal", type="master_shot",
            x=0, y=0, w=240, h=180,
            data={"masterShotAssetId": asset.id, "title": "Master"},
            status="done",
        )
        tgt = Node(
            shot_id=shot.id, short_id="mal2", type="image",
            x=0, y=0, w=240, h=180, data={"title": "shot"},
            status="idle",
        )
        s.add_all([master, tgt]); s.commit()
        for n in (master, tgt):
            s.refresh(n)
        s.add(Edge(shot_id=shot.id, source_id=master.id, target_id=tgt.id))
        s.commit()
        tgt_id = tgt.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(tgt_id)
    assert "establishing_shot_ref:" in captured["prompt"]


# ── Script + BibleRef passthrough ───────────────────────────────────────


@pytest.mark.asyncio
async def test_script_node_text_surfaces_in_user_message(client, monkeypatch):
    """ScriptNode (Phase 4) carries ``data.scriptText``. The upstream
    walk must surface it under a Script section so the LLM has the
    dramatic context driving this shot."""
    with get_session() as s:
        b = _make_shot(s, name="script-pass")
        script_node = Node(
            shot_id=b.id, short_id="scp", type="script",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Shot 3 script",
                "scriptText": "An quay đầu lại, đôi mắt mở to. Mưa đột nhiên rơi nặng hơn.",
            },
            status="idle",
        )
        char = Node(
            shot_id=b.id, short_id="sc2", type="character",
            x=0, y=0, w=240, h=180,
            data={"title": "An", "aiBrief": "young man, dark hair",
                  "mediaId": "uuuuuuuu-sc21-1111-2222-333333333333"},
            status="done",
        )
        tgt = Node(
            shot_id=b.id, short_id="sct", type="image",
            x=0, y=0, w=240, h=180, data={"title": "Reaction"},
            status="idle",
        )
        s.add_all([script_node, char, tgt]); s.commit()
        for n in (script_node, char, tgt):
            s.refresh(n)
        s.add(Edge(shot_id=b.id, source_id=script_node.id, target_id=tgt.id))
        s.add(Edge(shot_id=b.id, source_id=char.id, target_id=tgt.id))
        s.commit()
        tgt_id = tgt.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        return "Medium close-up of An, eyes wide, rain intensifies."

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(tgt_id)
    user = captured["prompt"]
    assert "Script (this shot" in user
    # Verbatim VN text preserved.
    assert "An quay đầu lại, đôi mắt mở to" in user
    # No ref_image_N for the script node (it has no media).
    assert "#scp" not in user


@pytest.mark.asyncio
async def test_bible_ref_node_text_surfaces_in_user_message(client, monkeypatch):
    """BibleRefNode carries ``data.bibleText`` — a snapshot of the bible
    loaded from the API at edit time. Surfaces under its own Bible
    reference section (distinct from the auto-injected Project/Scene
    bible block, which sources from DB at synth time)."""
    with get_session() as s:
        b = _make_shot(s, name="bibleref-pass")
        bref = Node(
            shot_id=b.id, short_id="bre", type="bible_ref",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Scene atmosphere",
                "bibleType": "scene",
                "bibleText": "Late evening, lone streetlight reflects on wet asphalt.",
            },
            status="idle",
        )
        char = Node(
            shot_id=b.id, short_id="brc", type="character",
            x=0, y=0, w=240, h=180,
            data={"title": "x", "aiBrief": "girl",
                  "mediaId": "uuuuuuuu-brc1-1111-2222-333333333333"},
            status="done",
        )
        tgt = Node(
            shot_id=b.id, short_id="brt", type="image",
            x=0, y=0, w=240, h=180, data={"title": "x"},
            status="idle",
        )
        s.add_all([bref, char, tgt]); s.commit()
        for n in (bref, char, tgt):
            s.refresh(n)
        s.add(Edge(shot_id=b.id, source_id=bref.id, target_id=tgt.id))
        s.add(Edge(shot_id=b.id, source_id=char.id, target_id=tgt.id))
        s.commit()
        tgt_id = tgt.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        return "Cel anime medium shot, streetlight reflection on asphalt."

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(tgt_id)
    user = captured["prompt"]
    assert "Bible reference" in user
    assert "lone streetlight" in user


# ── Multi-subject detection ─────────────────────────────────────────────


def _seed_couple_via_image_siblings() -> dict:
    with get_session() as s:
        b = _make_shot(s, name="couple-img")
        cm = Node(
            shot_id=b.id, short_id="cm1", type="character",
            x=0, y=0, w=240, h=180,
            data={"title": "M", "aiBrief": "young man, short hair",
                  "mediaId": "uuuuuuuu-cm11-1111-2222-333333333333"},
            status="done",
        )
        cf = Node(
            shot_id=b.id, short_id="cf1", type="character",
            x=0, y=0, w=240, h=180,
            data={"title": "F", "aiBrief": "young woman, twin braids",
                  "mediaId": "uuuuuuuu-cf11-1111-2222-333333333333"},
            status="done",
        )
        img_m = Node(
            shot_id=b.id, short_id="im1", type="image",
            x=0, y=0, w=240, h=180,
            data={"title": "M shot",
                  "mediaId": "uuuuuuuu-im11-1111-2222-333333333333"},
            status="done",
        )
        img_f = Node(
            shot_id=b.id, short_id="if1", type="image",
            x=0, y=0, w=240, h=180,
            data={"title": "F shot",
                  "mediaId": "uuuuuuuu-if11-1111-2222-333333333333"},
            status="done",
        )
        tgt = Node(
            shot_id=b.id, short_id="ct1", type="image",
            x=0, y=0, w=240, h=180,
            data={"title": "Two-shot"},
            status="idle",
        )
        s.add_all([cm, cf, img_m, img_f, tgt]); s.commit()
        for n in (cm, cf, img_m, img_f, tgt):
            s.refresh(n)
        s.add(Edge(shot_id=b.id, source_id=cm.id, target_id=img_m.id))
        s.add(Edge(shot_id=b.id, source_id=cf.id, target_id=img_f.id))
        s.add(Edge(shot_id=b.id, source_id=img_m.id, target_id=tgt.id))
        s.add(Edge(shot_id=b.id, source_id=img_f.id, target_id=tgt.id))
        s.commit()
        return {"target_id": tgt.id, "cm_short": cm.short_id, "cf_short": cf.short_id}


@pytest.mark.asyncio
async def test_multi_subject_detected_via_image_siblings(client, monkeypatch):
    ids = _seed_couple_via_image_siblings()
    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["system_prompt"] = system_prompt or ""
        captured["prompt"] = prompt
        return "Two-shot of ref_image_1 and ref_image_2."

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(ids["target_id"])
    sp = captured["system_prompt"]
    user = captured["prompt"]
    assert "MULTI-SUBJECT MODE" in sp
    assert "DISTINCT SUBJECTS DETECTED: 2 characters" in user
    assert "ref_image_1:" in user and "ref_image_2:" in user
    # Internal shortIds never leak (Google content classifier false-positive).
    assert f"#{ids['cm_short']}" not in user
    assert f"#{ids['cf_short']}" not in user


@pytest.mark.asyncio
async def test_single_subject_skips_multi_clause(client, monkeypatch):
    ids = _seed_simple_char_chain()
    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["system_prompt"] = system_prompt or ""
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(ids["target_id"])
    assert "MULTI-SUBJECT MODE" not in captured["system_prompt"]
    assert "DISTINCT SUBJECTS DETECTED" not in captured["prompt"]


@pytest.mark.asyncio
async def test_multi_subject_video_clause_when_two_chars_in_source(
    client, monkeypatch
):
    """Video target whose source frame was composed from 2+ chars must
    fire the multi-subject video clause (per-subject anti-freeze,
    asymmetric motion)."""
    with get_session() as s:
        b = _make_shot(s, name="vid-couple")
        cm = Node(
            shot_id=b.id, short_id="vcm", type="character",
            x=0, y=0, w=240, h=180,
            data={"title": "M", "aiBrief": "man",
                  "mediaId": "uuuuuuuu-vcm1-1111-2222-333333333333"},
            status="done",
        )
        cf = Node(
            shot_id=b.id, short_id="vcf", type="character",
            x=0, y=0, w=240, h=180,
            data={"title": "F", "aiBrief": "woman",
                  "mediaId": "uuuuuuuu-vcf1-1111-2222-333333333333"},
            status="done",
        )
        couple_img = Node(
            shot_id=b.id, short_id="vci", type="image",
            x=0, y=0, w=240, h=180,
            data={"title": "Two-shot still",
                  "aiBrief": "two characters side-by-side at night",
                  "mediaId": "uuuuuuuu-vci1-1111-2222-333333333333"},
            status="done",
        )
        vid = Node(
            shot_id=b.id, short_id="vcv", type="video",
            x=0, y=0, w=240, h=180,
            data={"title": "Clip"},
            status="idle",
        )
        s.add_all([cm, cf, couple_img, vid]); s.commit()
        for n in (cm, cf, couple_img, vid):
            s.refresh(n)
        s.add(Edge(shot_id=b.id, source_id=cm.id, target_id=couple_img.id))
        s.add(Edge(shot_id=b.id, source_id=cf.id, target_id=couple_img.id))
        s.add(Edge(shot_id=b.id, source_id=couple_img.id, target_id=vid.id))
        s.commit()
        vid_id = vid.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["system_prompt"] = system_prompt or ""
        return "ok"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(vid_id)
    sp = captured["system_prompt"]
    assert "MULTI-SUBJECT MODE" in sp
    assert "PER character" in sp


# ── Prompt vs aiBrief precedence ────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_wins_over_aibrief(client, monkeypatch):
    """When an upstream node carries both ``prompt`` and ``aiBrief``,
    the synthesiser must use ``prompt`` — that's the authoritative
    user-stamped text."""
    with get_session() as s:
        b = _make_shot(s, name="prompt-wins")
        upstream = Node(
            shot_id=b.id, short_id="pw", type="image",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Hero",
                "prompt": "AUTHORITATIVE-PROMPT-TEXT young woman walking through Tokyo street, cel anime",
                "aiBrief": "STALE-VISION-DESCRIPTION studio backdrop",
                "mediaId": "uuuuuuuu-pw11-1111-2222-333333333333",
            },
            status="done",
        )
        target = Node(
            shot_id=b.id, short_id="pwg", type="video",
            x=0, y=0, w=240, h=180, data={"title": "Motion"},
            status="idle",
        )
        s.add_all([upstream, target]); s.commit()
        s.refresh(upstream); s.refresh(target)
        s.add(Edge(shot_id=b.id, source_id=upstream.id, target_id=target.id))
        s.commit()
        tgt_id = target.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(tgt_id)
    user = captured["prompt"]
    assert "AUTHORITATIVE-PROMPT-TEXT" in user
    assert "STALE-VISION-DESCRIPTION" not in user


@pytest.mark.asyncio
async def test_aibrief_fallback_when_no_prompt(client, monkeypatch):
    """Upload-only node (aiBrief present, prompt absent) still surfaces
    aiBrief — without this the LLM has no description for the upload."""
    with get_session() as s:
        b = _make_shot(s, name="brief-fallback")
        upstream = Node(
            shot_id=b.id, short_id="bf", type="visual_asset",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Uploaded prop",
                "aiBrief": "BRIEF-FALLBACK weathered leather satchel with brass buckle",
                "mediaId": "uuuuuuuu-bf11-1111-2222-333333333333",
            },
            status="done",
        )
        target = Node(
            shot_id=b.id, short_id="bft", type="image",
            x=0, y=0, w=240, h=180, data={"title": "Pickup"},
            status="idle",
        )
        s.add_all([upstream, target]); s.commit()
        s.refresh(upstream); s.refresh(target)
        s.add(Edge(shot_id=b.id, source_id=upstream.id, target_id=target.id))
        s.commit()
        tgt_id = target.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt(tgt_id)
    assert "BRIEF-FALLBACK" in captured["prompt"]


# ── Bilingual VN → EN mock ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bilingual_vn_input_passes_through_to_llm(client, monkeypatch):
    """Vietnamese script_text upstream → user message preserves VN
    verbatim, system prompt explicitly instructs LLM to read VN
    natively and output English."""
    with get_session() as s:
        b = _make_shot(s, name="bilingual")
        script_node = Node(
            shot_id=b.id, short_id="bln", type="script",
            x=0, y=0, w=240, h=180,
            data={
                "title": "VN script",
                "scriptText": (
                    "Cảnh hoàng hôn trên sân thượng. An đứng cô đơn, "
                    "gió thổi mạnh, tóc bay về phía sau."
                ),
            },
            status="idle",
        )
        char = Node(
            shot_id=b.id, short_id="blc", type="character",
            x=0, y=0, w=240, h=180,
            data={"title": "An", "aiBrief": "young man, mid-20s",
                  "mediaId": "uuuuuuuu-blc1-1111-2222-333333333333"},
            status="done",
        )
        tgt = Node(
            shot_id=b.id, short_id="blt", type="image",
            x=0, y=0, w=240, h=180, data={"title": "Rooftop sunset"},
            status="idle",
        )
        s.add_all([script_node, char, tgt]); s.commit()
        for n in (script_node, char, tgt):
            s.refresh(n)
        s.add(Edge(shot_id=b.id, source_id=script_node.id, target_id=tgt.id))
        s.add(Edge(shot_id=b.id, source_id=char.id, target_id=tgt.id))
        s.commit()
        tgt_id = tgt.id

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["prompt"] = prompt
        captured["system_prompt"] = system_prompt or ""
        # Simulate provider obeying instruction: English output.
        return (
            "Wide establishing cel anime rooftop sunset, An standing alone, "
            "wind whips his hair back, golden hour rim light."
        )

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.auto_prompt(tgt_id)

    user = captured["prompt"]
    sp = captured["system_prompt"]
    # VN preserved verbatim in user message.
    assert "Cảnh hoàng hôn" in user
    # System prompt commits to bilingual contract.
    assert "vietnamese" in sp.lower()
    assert "english" in sp.lower()
    # Returned output is English.
    assert "rooftop sunset" in out.lower() or "wide establishing" in out.lower()
    assert "Cảnh" not in out  # no VN leakage in EN output


# ── Generic edge cases ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_prompt_no_upstream_falls_back_to_title(client, monkeypatch):
    """Bare image with no edges still gets a sensible prompt (title +
    anime style floor)."""
    with get_session() as s:
        b = _make_shot(s, name="bare")
        n = Node(
            shot_id=b.id, short_id="brr", type="image",
            x=0, y=0, w=240, h=180,
            data={"title": "Lone bicycle in rain"},
            status="idle",
        )
        s.add(n); s.commit(); s.refresh(n)
        nid = n.id

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        assert "lone bicycle" in prompt.lower() or "bicycle" in prompt.lower()
        return "Cel anime still of a lone bicycle in the rain."

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.auto_prompt(nid)
    assert "bicycle" in out.lower()


@pytest.mark.asyncio
async def test_auto_prompt_raises_for_unknown_node(client):
    with pytest.raises(prompt_synth.PromptSynthError):
        await prompt_synth.auto_prompt(999999)


@pytest.mark.asyncio
async def test_auto_prompt_caps_long_responses(client, monkeypatch):
    ids = _seed_simple_char_chain()
    long_text = "a" * 900

    async def stub_run(*a, **k):
        return long_text

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.auto_prompt(ids["target_id"])
    # Cap raised to 600 (anime prompts run denser than fashion).
    assert len(out) <= 601
    assert out.endswith("…")


# ── /api/prompt/auto routes ─────────────────────────────────────────────


def test_route_auto_happy_path(client, monkeypatch):
    ids = _seed_simple_char_chain()

    async def stub(node_id, *, camera=None):
        assert node_id == ids["target_id"]
        return "synthesized cel anime prompt"

    monkeypatch.setattr(prompt_synth, "auto_prompt", stub)
    r = client.post("/api/prompt/auto", json={"node_id": ids["target_id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["prompt"] == "synthesized cel anime prompt"
    assert body["node_id"] == ids["target_id"]


def test_route_auto_passes_camera_arg_through(client, monkeypatch):
    ids = _seed_simple_char_chain()
    captured: dict = {}

    async def stub(node_id, *, camera=None):
        captured["camera"] = camera
        return "ok"

    monkeypatch.setattr(prompt_synth, "auto_prompt", stub)
    r = client.post(
        "/api/prompt/auto",
        json={"node_id": ids["target_id"], "camera": "static"},
    )
    assert r.status_code == 200, r.text
    assert captured["camera"] == "static"


def test_route_auto_502_on_synth_failure(client, monkeypatch):
    async def stub(node_id, *, camera=None):
        raise prompt_synth.PromptSynthError("provider timeout")

    monkeypatch.setattr(prompt_synth, "auto_prompt", stub)
    r = client.post("/api/prompt/auto", json={"node_id": 1})
    assert r.status_code == 502


# ── /api/prompt/auto-batch ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_prompt_batch_returns_distinct_prompts(client, monkeypatch):
    """Batch mode asks the provider for a JSON array of N variants."""
    ids = _seed_simple_char_chain()
    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["system_prompt"] = system_prompt or ""
        return _json.dumps([
            "Wide establishing cel anime, character at center",
            "Medium close-up, low-angle hero composition",
            "Over-the-shoulder reverse, rain on glass",
            "Extreme close-up, eye reflecting neon",
        ])

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.auto_prompt_batch(ids["target_id"], 4)
    assert len(out) == 4
    assert len(set(out)) == 4
    sp = captured["system_prompt"].lower()
    assert "batch mode" in sp
    assert "exactly 4" in sp
    assert "json array" in sp


@pytest.mark.asyncio
async def test_auto_prompt_batch_count_1_falls_through(client, monkeypatch):
    ids = _seed_simple_char_chain()

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return "single prompt"

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.auto_prompt_batch(ids["target_id"], 1)
    assert out == ["single prompt"]


@pytest.mark.asyncio
async def test_auto_prompt_batch_strips_markdown_fences(client, monkeypatch):
    ids = _seed_simple_char_chain()

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return '```json\n["a", "b"]\n```'

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.auto_prompt_batch(ids["target_id"], 2)
    assert out == ["a", "b"]


@pytest.mark.asyncio
async def test_auto_prompt_batch_pads_short_response(client, monkeypatch):
    ids = _seed_simple_char_chain()

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return '["only-one"]'

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.auto_prompt_batch(ids["target_id"], 3)
    assert out == ["only-one", "only-one", "only-one"]


def test_route_auto_batch_passes_through(client, monkeypatch):
    ids = _seed_simple_char_chain()
    captured: dict = {}

    async def stub(node_id, count, *, camera=None):
        captured["count"] = count
        return [f"prompt-{i}" for i in range(count)]

    monkeypatch.setattr(prompt_synth, "auto_prompt_batch", stub)
    r = client.post(
        "/api/prompt/auto-batch",
        json={"node_id": ids["target_id"], "count": 4},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["prompts"]) == 4
    assert captured["count"] == 4


def test_route_auto_batch_rejects_bad_count(client):
    r = client.post(
        "/api/prompt/auto-batch",
        json={"node_id": 1, "count": 0},
    )
    assert r.status_code == 400


# ── Storyboard (anime narrative beats) ──────────────────────────────────


def _seed_storyboard_target(narrative_seed: str = "") -> int:
    with get_session() as s:
        b = _make_shot(s, name="sb")
        char = Node(
            shot_id=b.id, short_id="sbc", type="character",
            x=0, y=0, w=240, h=180,
            data={
                "title": "Hero",
                "aiBrief": "young woman, twin braids, cel-shaded",
                "mediaId": "uuuuuuuu-sbc1-1111-2222-333333333333",
            },
            status="done",
        )
        target = Node(
            shot_id=b.id, short_id="sbt", type="storyboard",
            x=0, y=0, w=240, h=180,
            data={"title": "Beats", "narrativeSeed": narrative_seed},
            status="idle",
        )
        s.add_all([char, target]); s.commit()
        for n in (char, target):
            s.refresh(n)
        s.add(Edge(shot_id=b.id, source_id=char.id, target_id=target.id))
        s.commit()
        return target.id


@pytest.mark.asyncio
async def test_storyboard_suffix_carries_anime_narrative_language(
    client, monkeypatch
):
    """The anime rewrite drops fashion-era unbox/try-on vocab and pivots
    to character-reaction / cut-to-next-angle / environmental-detail
    beats."""
    tgt = _seed_storyboard_target(narrative_seed="character meets a stranger")
    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["system_prompt"] = system_prompt or ""
        return _json.dumps({
            "prompts": ["a", "b", "c", "d"],
            "parents": [None, 0, 1, 2],
        })

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    await prompt_synth.auto_prompt_storyboard(
        tgt, count=4, narrative_seed="character meets a stranger"
    )
    sp = captured["system_prompt"]
    assert "STORYBOARD MODE" in sp
    # Anime narrative vocabulary.
    assert "character reaction" in sp.lower() or "reaction shot" in sp.lower() \
        or "character reaction" in sp.lower()
    assert "anime cel-shaded" in sp.lower() or "cel-shaded" in sp.lower()
    # Narrative seed injected.
    assert "character meets a stranger" in sp
    # Fashion-era vocab is gone.
    assert "unbox" not in sp.lower()
    assert "try-on" not in sp.lower()
    assert "selfie" not in sp.lower()


@pytest.mark.asyncio
async def test_storyboard_returns_object_with_prompts_and_parents(
    client, monkeypatch
):
    tgt = _seed_storyboard_target()
    payload = {
        "prompts": [f"Beat {i}" for i in range(1, 6)],
        "parents": [None, 0, 1, 2, 3],
    }

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return _json.dumps(payload)

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.auto_prompt_storyboard(tgt, count=5)
    assert out["prompts"] == payload["prompts"]
    assert out["parents"] == payload["parents"]


@pytest.mark.asyncio
async def test_storyboard_strips_markdown_fences(client, monkeypatch):
    tgt = _seed_storyboard_target()

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return (
            "```json\n"
            + _json.dumps({"prompts": ["a", "b"], "parents": [None, 0]})
            + "\n```"
        )

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.auto_prompt_storyboard(tgt, count=2)
    assert out == {"prompts": ["a", "b"], "parents": [None, 0]}


@pytest.mark.asyncio
async def test_storyboard_rejects_parents_zero_non_null(client, monkeypatch):
    tgt = _seed_storyboard_target()

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return _json.dumps({"prompts": ["a", "b"], "parents": [0, 0]})

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    with pytest.raises(prompt_synth.PromptSynthError, match="parents\\[0\\]"):
        await prompt_synth.auto_prompt_storyboard(tgt, count=2)


@pytest.mark.asyncio
async def test_storyboard_rejects_parent_out_of_range(client, monkeypatch):
    tgt = _seed_storyboard_target()

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return _json.dumps({"prompts": ["a", "b", "c"], "parents": [None, 0, 5]})

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    with pytest.raises(prompt_synth.PromptSynthError, match="parents\\[2\\]"):
        await prompt_synth.auto_prompt_storyboard(tgt, count=3)


@pytest.mark.asyncio
async def test_storyboard_rejects_length_mismatch(client, monkeypatch):
    tgt = _seed_storyboard_target()

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return _json.dumps({
            "prompts": ["a", "b", "c", "d"],
            "parents": [None, 0, 1],
        })

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    with pytest.raises(prompt_synth.PromptSynthError, match="length mismatch"):
        await prompt_synth.auto_prompt_storyboard(tgt, count=4)


@pytest.mark.asyncio
async def test_storyboard_rejects_count_out_of_range(client, monkeypatch):
    tgt = _seed_storyboard_target()
    with pytest.raises(prompt_synth.PromptSynthError, match="1\\.\\.8"):
        await prompt_synth.auto_prompt_storyboard(tgt, count=0)
    with pytest.raises(prompt_synth.PromptSynthError, match="1\\.\\.8"):
        await prompt_synth.auto_prompt_storyboard(tgt, count=9)


# ── /api/prompt/parse-script ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_script_returns_structured_shots(client, monkeypatch):
    """LLM is mocked to return the strict-JSON output the system prompt
    requests. parse_script normalises the result + validates required
    fields per shot."""
    scene_id = uuid.uuid4()

    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["system_prompt"] = system_prompt or ""
        captured["prompt"] = prompt
        return _json.dumps({
            "shots": [
                {
                    "order": 1,
                    "script_text": "An đứng giữa quảng trường.",
                    "camera_angle": "wide establishing",
                    "characters_in_frame": ["An"],
                    "environment": "city square at dusk",
                    "dialogue": None,
                    "beat_notes": "establish the scene's main location",
                },
                {
                    "order": 2,
                    "script_text": "An: \"Tôi vẫn còn đứng đây.\"",
                    "camera_angle": "close-up",
                    "characters_in_frame": ["An"],
                    "environment": "city square at dusk",
                    "dialogue": "An: \"Tôi vẫn còn đứng đây.\"",
                    "beat_notes": "dialogue beat — establishing resolve",
                },
            ]
        })

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.parse_script(
        scene_id, "An đứng giữa quảng trường. An nói: 'Tôi vẫn còn đứng đây.'"
    )
    assert len(out) == 2
    # System prompt is the script-parse one.
    sp = captured["system_prompt"]
    assert "storyboard supervisor" in sp.lower()
    assert "strict json" in sp.lower() or "strict JSON" in sp
    # VN input passed through verbatim.
    assert "An đứng giữa quảng trường" in captured["prompt"]
    # Output shape.
    assert out[0]["order"] == 1
    assert out[0]["script_text"] == "An đứng giữa quảng trường."
    assert out[0]["camera_angle"] == "wide establishing"
    assert out[0]["characters_in_frame"] == ["An"]
    assert out[0]["dialogue"] is None
    assert out[1]["dialogue"] == 'An: "Tôi vẫn còn đứng đây."'


@pytest.mark.asyncio
async def test_parse_script_strips_markdown_fences(client, monkeypatch):
    scene_id = uuid.uuid4()

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return (
            "```json\n"
            + _json.dumps({
                "shots": [
                    {
                        "order": 1,
                        "script_text": "x",
                        "camera_angle": "medium",
                        "characters_in_frame": [],
                        "environment": "interior",
                        "dialogue": None,
                        "beat_notes": "",
                    }
                ]
            })
            + "\n```"
        )

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    out = await prompt_synth.parse_script(scene_id, "x")
    assert len(out) == 1


@pytest.mark.asyncio
async def test_parse_script_rejects_empty_input(client):
    with pytest.raises(prompt_synth.PromptSynthError, match="empty"):
        await prompt_synth.parse_script(uuid.uuid4(), "   ")


@pytest.mark.asyncio
async def test_parse_script_rejects_non_object_response(client, monkeypatch):
    scene_id = uuid.uuid4()

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return _json.dumps([])

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    with pytest.raises(prompt_synth.PromptSynthError, match="not a JSON object"):
        await prompt_synth.parse_script(scene_id, "x")


@pytest.mark.asyncio
async def test_parse_script_rejects_missing_shots(client, monkeypatch):
    scene_id = uuid.uuid4()

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        return _json.dumps({"foo": "bar"})

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    with pytest.raises(prompt_synth.PromptSynthError, match="shots"):
        await prompt_synth.parse_script(scene_id, "x")


def test_route_parse_script_happy_path(client, monkeypatch):
    scene_id = uuid.uuid4()

    async def stub(sid, script_text):
        assert sid == scene_id
        return [
            {
                "order": 1,
                "script_text": "An đứng giữa quảng trường.",
                "camera_angle": "wide establishing",
                "characters_in_frame": ["An"],
                "environment": "city square at dusk",
                "dialogue": None,
                "beat_notes": "establish",
            }
        ]

    monkeypatch.setattr(prompt_synth, "parse_script", stub)
    r = client.post(
        "/api/prompt/parse-script",
        json={"scene_id": str(scene_id), "script_text": "An đứng..."},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scene_id"] == str(scene_id)
    assert len(body["shots"]) == 1
    assert body["shots"][0]["camera_angle"] == "wide establishing"


def test_route_parse_script_rejects_empty(client):
    r = client.post(
        "/api/prompt/parse-script",
        json={"scene_id": str(uuid.uuid4()), "script_text": "   "},
    )
    assert r.status_code == 400


def test_route_parse_script_502_on_synth_failure(client, monkeypatch):
    scene_id = uuid.uuid4()

    async def stub(sid, script_text):
        raise prompt_synth.PromptSynthError("provider failed")

    monkeypatch.setattr(prompt_synth, "parse_script", stub)
    r = client.post(
        "/api/prompt/parse-script",
        json={"scene_id": str(scene_id), "script_text": "some script"},
    )
    assert r.status_code == 502
