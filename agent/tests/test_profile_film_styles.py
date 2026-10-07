"""Approved masters work across subjects and all app entry points without sample refs."""
import json
from pathlib import Path

import pytest

from flowboard.routes.automation import PlateBody, VideoPromptBody
from flowboard.services import automation, film_styles, production_run, prompt_writer, source_film
from flowboard.services.video_analyzer.production import build_asset_prompt


@pytest.mark.parametrize("key", film_styles.PROFILE_KEYS)
def test_catalog_and_app_routes_use_exact_profile_master(client, key):
    catalog = {p["key"]: p for p in client.get("/api/automation/styles").json()}
    meta = catalog[key]
    assert meta["reference_mode"] == "profile_text"
    assert meta["character_reference"] is None and meta["environment_reference"] is None
    assert client.get(f"/api/automation/styles/{key}/character").status_code == 404
    assert source_film.FilmOptions(style=key).style == key
    assert automation.style_from_rules(meta["visual_style"] + " Not anime, not CGI") == key
    assert source_film.source_rules(key)["visual_style"] == meta["visual_style"]
    for kind, profile in [
        ("character", {"name": "Rin", "age": 61, "hair": "short silver curls", "wardrobe": "worn ochre overalls"}),
        ("environment", {"name": "Old forest railway", "time_of_day": "dawn", "architecture": "timber station"}),
    ]:
        response = client.post("/api/automation/prompt", json={"kind": kind, kind: profile, "style": key})
        assert response.status_code == 200, response.text
        prompt = response.json()["prompt"]
        master = (film_styles.ROOT.parent / key / f"{kind}-master.txt").read_text()
        assert master.replace("{{PROFILE}}", json.dumps(profile, ensure_ascii=False, indent=2)) in prompt
        assert "{{" not in prompt and "@image" not in prompt
        assert "Ava" not in prompt and "Theo" not in prompt and "Campus hallway" not in prompt
        assert "For video," not in prompt


def test_frontend_catalog_matches_canonical_asset_text():
    root = Path(__file__).resolve().parents[2]
    ui = json.loads((root / "frontend/src/automation/approved-film-styles.json").read_text())
    assert {p["key"] for p in ui} == set(film_styles.PROFILE_KEYS)
    for preset in ui:
        meta = film_styles.metadata(preset["key"])
        assert preset["label"] == meta["label"]
        assert preset["text"] == meta["visual_style"]


@pytest.mark.parametrize("key", film_styles.PROFILE_KEYS)
@pytest.mark.asyncio
async def test_selected_profile_identity_and_state_survive_without_legacy_rewrite(monkeypatch, key):
    async def forbidden(*a, **kw):
        raise AssertionError("Approved image master must not be rewritten")
    monkeypatch.setattr(prompt_writer.adapt_mod, "ask_json", forbidden)
    prompt = automation.build_character_prompt(
        {"name": "Rin", "age": 11, "states": [{"key": "unused-summer"}], "plate": {"url": "OLD_IMAGE"}},
        {"key": "winter", "wardrobe": "red wool coat"}, has_reference=True, style=key)
    assert '@image1' in prompt and '@image2' not in prompt
    assert '"age": 11' in prompt and "red wool coat" in prompt
    assert "unused-summer" not in prompt and "OLD_IMAGE" not in prompt
    assert "there is no input image" not in prompt and "Text-only input" not in prompt
    output, by = await prompt_writer.write_image_prompt(prompt, kind="character", subject="Rin", design={"hair": "black"}, style=key)
    assert output == prompt and by == film_styles.version(key)


@pytest.mark.parametrize("key", film_styles.PROFILE_KEYS)
def test_batch_dependencies_and_video_style_do_not_fall_back(key):
    deps = [{"asset_id": "hero", "name": "Rin", "reference_url": "https://a"},
            {"asset_id": "same", "name": "Rin alternate", "reference_url": "https://a"},
            {"asset_id": "other", "name": "Other", "reference_url": "https://b"}]
    payload = production_run.material_payload(
        {"kind": "asset", "item": {"kind": "prop", "name": "Portrait"}, "plate": {}, "slot": "plate"},
        deps, {"style": key, "aspectRatio": "1:1", "imageModel": "dola-seedream-5-0-pro"})
    assert payload["reference_urls"] == ["https://a", "https://b"]
    assert payload["aspect_ratio"] == "16:9" and payload["style_version"] == film_styles.version(key)
    assert "@image1 = Rin" in payload["prompt"] and "@image2 = Other" in payload["prompt"]
    assert "@image3" not in payload["prompt"]
    PlateBody.model_validate(payload)
    VideoPromptBody(sequence={}, style=key)
    group = build_asset_prompt({"name": "Fishermen", "description": "blue wool coats"}, kind="background_group", style=key)
    assert "blue wool coats" in group and "@image" not in group
    context = prompt_writer._payload({}, [], [], 5, characters=[], environment=None, look=key,
        aspect_ratio="1:1", previous_state="", unsafe={}, school_age=False, style_note="OLD STYLE MUST NOT WIN")
    assert context["clip"]["style"] == film_styles.video_style(key)
    assert "For video," in context["clip"]["style"]
    assert len({production_run.input_version({"style": k}) for k in film_styles.PROFILE_KEYS}) == 3


@pytest.mark.parametrize("key", film_styles.PROFILE_KEYS)
@pytest.mark.asyncio
async def test_seedream_receives_no_bundled_images_and_keeps_explicit_identity_slot(monkeypatch, key):
    calls = []
    monkeypatch.setattr(automation.avis_api, "is_configured", lambda: True)
    async def fetch(urls):
        return [b"identity"] if urls else []
    async def generate(prompt, refs, **kw):
        calls.append((refs, kw))
        return [b"generated"]
    monkeypatch.setattr(automation, "_fetch_reference_bytes", fetch)
    monkeypatch.setattr(automation.avis_api, "generate_image_variants", generate)
    monkeypatch.setattr(automation, "_publish", lambda data: "https://output")
    monkeypatch.setattr(automation, "_ingest_plate", lambda data: "media")
    kwargs = dict(style=key, material_kind="character", style_version=film_styles.version(key),
                  image_model="dola-seedream-5-0-pro", aspect_ratio="1:1")
    await automation.generate_plate("Approved", **kwargs)
    await automation.generate_plate("Approved with identity", reference_urls=["https://identity"], **kwargs)
    assert calls[0][0] is None and calls[1][0] == [b"identity"]
    assert calls[0][1]["aspect_ratio"] == "16:9"
    with pytest.raises(automation.AutomationError, match="slots were not shifted"):
        await automation.generate_plate("Approved", reference_urls=["https://a", "https://missing"], **kwargs)
    with pytest.raises(automation.AutomationError, match="preset changed"):
        await automation.generate_plate("Approved", **{**kwargs, "style_version": "stale"})
    assert len(calls) == 2


def test_multiple_outfits_require_selected_or_baseline_state(client):
    body = {"kind": "character", "style": "anime_jp_modern", "character": {
        "name": "Rin", "states": [{"key": "day", "wardrobe": "ochre coat"},
                                   {"key": "night", "wardrobe": "green coat"}]}}
    assert client.post("/api/automation/prompt", json=body).status_code == 422
    body["character"]["baseline_state_key"] = "day"
    prompt = client.post("/api/automation/prompt", json=body).json()["prompt"]
    assert "ochre coat" in prompt and "green coat" not in prompt
def test_sheet_profile_keeps_design_but_not_temporal_source_records():
    from flowboard.services import film_styles
    profile = film_styles.profile_data({
        'name': 'Box', 'description': 'Small red box',
        'design': {'reference_variants': ['Closed three-quarter reference']},
        'observed_states': [{'shot': 1, 'visibility': 'offscreen', 'holder_id': 'hero'}],
    })
    assert 'observed_states' not in profile
    assert profile['description'] == 'Small red box'
    assert profile['design']['reference_variants'] == ['Closed three-quarter reference']
