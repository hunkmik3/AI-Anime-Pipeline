"""Phase 8.1 — Manual mode + per-ref @image label assignment.

Covers:
  - order_refs_by_label pure helper (positional + mixed labeled/unlabeled)
  - worker _handle_gen_video: verbatim prompt (no synth/bible) + label-driven
    reference_image ordering on the Seedance 2.0 (dreamina) path
  - auto_prompt manual-mode guard (skip Phase 6 synth) + _format_user_message
    skip_bible flag (skip Bible inject)
  - Automation-mode regression guard (synth + bible still run)
  - prompt_mode / reference_label / reference_description persist round-trips

Dreamina HTTP traffic is mocked with httpx.MockTransport, matching the
pattern in test_video_provider_dreamina.py. LLM routing is bypassed by
patching ``run_llm`` at the prompt_synth import boundary.
"""
from __future__ import annotations

import json

import httpx
import pytest

from flowboard.db import get_session
from flowboard.db.models import Node, Project, Scene, Shot
from flowboard.services import prompt_synth
from flowboard.services.llm import secrets
from flowboard.services.video import dreamina, registry as _r
from flowboard.services.video.ref_ordering import order_refs_by_label
from flowboard.worker import processor as proc
from tests.conftest import make_shot


# ── fixtures (mirror test_video_provider_dreamina.py) ────────────────────


@pytest.fixture
def _dreamina_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWBOARD_SECRETS_PATH", str(tmp_path / "secrets.json"))
    secrets.set_api_key("dreamina", "ark-test-key")
    _r.register_defaults()
    yield
    dreamina.reset_http_client_factory()


def _factory_with_handler(handler):
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, timeout=5.0)

    return factory


def _submit_poll_download_handler(seen_bodies: list[dict]):
    """A handler covering the full submit → poll(succeeded) → download cycle
    so ``run_to_completion`` returns a terminal success. Captures every
    submit body into ``seen_bodies``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/tasks"):
            seen_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "cgt-phase8-test"})
        if request.method == "GET" and request.url.path.endswith("/tasks/cgt-phase8-test"):
            return httpx.Response(200, json={
                "id": "cgt-phase8-test",
                "model": "dreamina-seedance-2-0-260128",
                "status": "succeeded",
                "content": {"video_url": "https://signed.example/clip.mp4"},
                "usage": {"completion_tokens": 108900, "total_tokens": 108900},
                "duration": 5, "resolution": "720p", "ratio": "16:9",
                "framespersecond": 24, "seed": 1,
            })
        if request.url.host == "signed.example":
            return httpx.Response(200, content=b"FAKE_MP4" * 50)
        return httpx.Response(500, json={"error": "unexpected"})

    return handler


# ── 1-2 / 5: order_refs_by_label pure helper ─────────────────────────────


def test_manual_mode_resolves_label_to_positional_order():
    """Labels carry the @imageN digit; refs reorder ascending by that digit
    regardless of input (edge) order. @image2 before @image1 → swapped."""
    refs = ["urlA", "urlB"]
    labels = ["@image2", "@image1"]
    assert order_refs_by_label(refs, labels) == ["urlB", "urlA"]


def test_manual_mode_mixed_labeled_unlabeled_refs():
    """Labeled refs (by digit) lead; unlabeled / no-digit labels (e.g.
    @kenji, empty) keep edge order and are appended after."""
    refs = ["a", "b", "c", "d"]
    labels = ["@image3", None, "@kenji", "@image1"]
    # labeled: d(@image1), a(@image3); unlabeled in edge order: b, c(@kenji)
    assert order_refs_by_label(refs, labels) == ["d", "a", "b", "c"]


def test_order_refs_by_label_all_null_is_noop():
    """All-null labels (Automation / label-less canvas) → input order kept."""
    refs = ["x", "y", "z"]
    assert order_refs_by_label(refs, [None, None, None]) == refs
    # Missing/short labels list is padded with None, not a crash.
    assert order_refs_by_label(refs, []) == refs


# ── 3: worker sends the pasted prompt verbatim (no synth, no bible) ──────


@pytest.mark.asyncio
async def test_manual_mode_uses_textarea_content_as_prompt(_dreamina_env):
    """A manually-pasted prompt reaches the API text block verbatim — only
    the --rt/--rs inline flags are appended; no LLM rewrite, no Bible."""
    seen_bodies: list[dict] = []
    dreamina.set_http_client_factory(
        _factory_with_handler(_submit_poll_download_handler(seen_bodies))
    )

    pasted = (
        "References: @image1 = Kenji, @image2 = Ren. "
        "Visual Style: Makoto Shinkai. Shot 1 (0-5s): they walk."
    )
    result, err = await proc._handle_gen_video({
        "model_id": "seedance-2-0",
        "motion_prompt": pasted,
        "reference_images": ["https://e/kenji.png", "https://e/ren.png"],
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "project_id": "8b62385c-4916-4abd-b01f-b28173d8eb04",
    })

    assert err is None, result
    text_block = next(b for b in seen_bodies[0]["content"] if b["type"] == "text")
    # Prompt already carries @imageN tags → no positional tags prepended;
    # only the inline aspect/resolution flags are appended.
    assert text_block["text"] == f"{pasted} --rt 16:9 --rs 720p"


# ── 4: worker reorders reference_image blocks by label (wiring) ──────────


@pytest.mark.asyncio
async def test_manual_mode_worker_reorders_refs_by_label(_dreamina_env):
    """The reference_labels param flows through the worker and reorders the
    reference_image content blocks before submit so @imageN binds right."""
    seen_bodies: list[dict] = []
    dreamina.set_http_client_factory(
        _factory_with_handler(_submit_poll_download_handler(seen_bodies))
    )

    # Edge order puts ren first, but ren is @image2 and kenji is @image1.
    result, err = await proc._handle_gen_video({
        "model_id": "seedance-2-0",
        "motion_prompt": "@image1 and @image2 walk together",
        "reference_images": ["https://e/ren.png", "https://e/kenji.png"],
        "reference_labels": ["@image2", "@image1"],
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "project_id": "8b62385c-4916-4abd-b01f-b28173d8eb04",
    })

    assert err is None, result
    image_blocks = [b for b in seen_bodies[0]["content"] if b["type"] == "image_url"]
    assert all(b.get("role") == "reference_image" for b in image_blocks)
    # kenji (@image1) must now be the FIRST reference_image block.
    assert [b["image_url"]["url"] for b in image_blocks] == [
        "https://e/kenji.png",
        "https://e/ren.png",
    ]


# ── helpers for the prompt_synth tests ───────────────────────────────────


def _make_video_target(prompt_mode: str | None) -> int:
    """character → video target chain; set prompt_mode on the video node."""
    with get_session() as s:
        project = Project(name="p8", project_bible={"art_style": "cel-shaded anime"})
        s.add(project)
        s.flush()
        scene = Scene(project_id=project.id, name="S1", order_index=0)
        s.add(scene)
        s.flush()
        shot = Shot(scene_id=scene.id, order_index=0)
        s.add(shot)
        s.flush()
        vdata: dict = {"title": "Vid"}
        if prompt_mode is not None:
            vdata["prompt_mode"] = prompt_mode
        vid = Node(
            shot_id=shot.id, short_id="vid", type="video",
            x=0, y=0, w=240, h=180, data=vdata, status="idle",
        )
        s.add(vid)
        s.commit()
        s.refresh(vid)
        return vid.id


# ── 6 (skips_phase6_synth) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_mode_skips_phase6_synth(client, monkeypatch):
    """auto_prompt on an explicit manual VideoNode must refuse — no LLM call,
    raises manual_mode_no_synth (server-side enforcement of 'skip Phase 6')."""
    called = {"n": 0}

    async def spy_run(*a, **k):
        called["n"] += 1
        return "should not be called"

    monkeypatch.setattr(prompt_synth, "run_llm", spy_run)
    vid_id = _make_video_target("manual")

    with pytest.raises(prompt_synth.PromptSynthError) as exc:
        await prompt_synth.auto_prompt(vid_id)
    assert "manual_mode_no_synth" in str(exc.value)
    assert called["n"] == 0


# ── 7 (skips_bible_inject) ───────────────────────────────────────────────


def test_manual_mode_skips_bible_inject():
    """_format_user_message(skip_bible=True) omits the Project/Scene Bible
    block that it otherwise prepends."""
    project_bible = {
        "art_style": "cel-shaded anime",
        "color_palette": ["warm tungsten"],
    }
    scene_bible_text = "Interior office, window camera-left."

    class _FakeTarget:
        type = "video"
        data: dict = {}

    target = _FakeTarget()
    with_bible = prompt_synth._format_user_message(
        [], target, project_bible, scene_bible_text, skip_bible=False
    )
    without_bible = prompt_synth._format_user_message(
        [], target, project_bible, scene_bible_text, skip_bible=True
    )

    # The bible content appears only when not skipped.
    assert "cel-shaded anime" in with_bible
    assert "window camera-left" in with_bible.lower() or "camera-left" in with_bible
    assert "cel-shaded anime" not in without_bible
    assert "camera-left" not in without_bible


# ── 8 (automation_mode_unchanged) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_automation_mode_unchanged(client, monkeypatch):
    """A node with prompt_mode='automation' still runs Phase 6 synth AND
    injects the PROJECT Bible — the manual guard must not fire.
    (Phase 8.3: Scene Bible removed, so only the project bible is asserted.)"""
    captured: dict = {}

    async def stub_run(feature, prompt, *, system_prompt=None, timeout=0):
        captured["user_msg"] = prompt
        return "Slow push-in on the office."

    monkeypatch.setattr(prompt_synth, "run_llm", stub_run)
    vid_id = _make_video_target("automation")

    out = await prompt_synth.auto_prompt(vid_id)
    assert out  # synth ran and returned text
    # Project bible injected into the user message (regression guard).
    assert "cel-shaded anime" in captured["user_msg"]


# ── 9-12: persist round-trips ────────────────────────────────────────────


def test_video_node_prompt_mode_persist(client):
    b = make_shot(client)
    n = client.post(
        "/api/nodes", json={"shot_id": b["id"], "type": "video"}
    ).json()
    client.patch(f"/api/nodes/{n['id']}", json={"data": {"prompt_mode": "manual"}})
    got = client.get(f"/api/shots/{b['id']}/workflow").json()
    node = next(x for x in got["nodes"] if x["id"] == n["id"])
    assert node["data"]["prompt_mode"] == "manual"


def test_character_node_reference_label_persist(client):
    b = make_shot(client)
    n = client.post(
        "/api/nodes", json={"shot_id": b["id"], "type": "character"}
    ).json()
    client.patch(
        f"/api/nodes/{n['id']}", json={"data": {"reference_label": "@kenji"}}
    )
    got = client.get(f"/api/shots/{b['id']}/workflow").json()
    node = next(x for x in got["nodes"] if x["id"] == n["id"])
    assert node["data"]["reference_label"] == "@kenji"


def test_character_node_reference_description_persist(client):
    b = make_shot(client)
    n = client.post(
        "/api/nodes", json={"shot_id": b["id"], "type": "character"}
    ).json()
    desc = "KENJI: tall, black suit, amber eyes."
    client.patch(
        f"/api/nodes/{n['id']}", json={"data": {"reference_description": desc}}
    )
    got = client.get(f"/api/shots/{b['id']}/workflow").json()
    node = next(x for x in got["nodes"] if x["id"] == n["id"])
    assert node["data"]["reference_description"] == desc


def test_visual_asset_node_reference_label_persist(client):
    b = make_shot(client)
    n = client.post(
        "/api/nodes", json={"shot_id": b["id"], "type": "visual_asset"}
    ).json()
    client.patch(
        f"/api/nodes/{n['id']}", json={"data": {"reference_label": "@office"}}
    )
    got = client.get(f"/api/shots/{b['id']}/workflow").json()
    node = next(x for x in got["nodes"] if x["id"] == n["id"])
    assert node["data"]["reference_label"] == "@office"
