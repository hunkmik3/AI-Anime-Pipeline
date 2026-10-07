import pytest
from flowboard.services import automation, film_styles as style, production_run, prompt_writer
from flowboard.services.video_analyzer.production import build_asset_prompt
from flowboard.routes.automation import PlateBody, ProductionRunConfig


def test_preset_routes_and_identity_are_not_sample_film_data(client):
    meta = client.get("/api/automation/styles").json()[0]
    assert meta["key"] == style.KEY and meta["image_model"] == "dola-seedream-5-0-pro"
    assert (
        client.get(meta["character_reference"]).content
        == style.reference_path("character").read_bytes()
    )
    assert client.get("/api/automation/styles/nope/character").status_code == 404
    assert automation.style_from_rules(style.RULE_TEXT) == style.KEY
    # Negative words like 'not anime' cannot turn this exact preset into anime.
    assert automation.style_from_rules(style.RULE_TEXT + " Not anime") == style.KEY
    assert automation._style(style.KEY)["video_style"] == style.VIDEO_STYLE
    assert ProductionRunConfig(resolution="480p").resolution == "480p"


def test_master_profiles_layout_age_and_state():
    char = {
        "name": "Lina",
        "age": 11,
        "species": "human",
        "states": [{"key": "winter"}, {"key": "summer"}],
        "design": {"hair": "black braids"},
        "plate": {"url": "SHOULD_NOT_LEAK"},
    }
    prompt = automation.build_character_prompt(
        char,
        {"key": "winter", "wardrobe": "red wool coat"},
        style=style.KEY,
        has_reference=True,
        design=char["design"],
    )
    assert "@image1" in prompt and "@image2" in prompt and "LEFT 60%" in prompt
    assert '"age": 11' in prompt and "red wool coat" in prompt and "summer" not in prompt
    assert "SHOULD_NOT_LEAK" not in prompt and "Theo" not in prompt and "Ava" not in prompt
    assert "Use mature adult proportions" not in prompt
    env = automation.build_environment_prompt(
        {"name": "Forest shrine", "time_of_day": "dawn"}, style=style.KEY
    )
    assert "Forest shrine" in env and "dawn" in env and "One single 16:9" in env
    assert "Do not copy exact architecture" in env


def test_environment_prompt_route_retains_additional_location_anchor(client):
    response = client.post(
        "/api/automation/prompt",
        json={
            "kind": "environment",
            "environment": {"name": "Mountain observatory"},
            "style": style.KEY,
            "has_reference": True,
        },
    )
    assert response.status_code == 200, response.text
    assert "@image2" in response.json()["prompt"]


def test_source_language_is_consistent_in_shot_package_policy():
    from tests.test_shot_package import fixture
    from flowboard.services import shot_package

    board = fixture()
    board["dialogueLanguage"] = "source"
    board["nodes"][-1]["data"]["shots"][0]["dialogue"] = [
        {"who": "HERO", "line": "Xin chào. 你好。"}
    ]
    package = shot_package.build(board, "another-film", "c1")
    assert package["dialogue_language"] == "source"
    assert "Keep all dialogue in English" not in package["policy"]
    assert not any(i["code"] == "dialogue_not_english" for i in package["issues"])


def test_prop_member_dependencies_start_after_style_and_deduplicate():
    deps = [
        {"id": "a", "name": "Hero", "ref_url": "https://a"},
        {"id": "b", "name": "Companion", "ref_url": "https://a"},
        {"id": "c", "name": "Box", "ref_url": "https://c"},
    ]
    p = build_asset_prompt(
        {"name": "Family photo", "dependency_references": deps}, kind="prop", style=style.KEY
    )
    assert "@image2 = Hero" in p and "@image2 = Companion" in p and "@image3 = Box" in p
    payload = production_run.material_payload(
        {
            "item": {"name": "Hero", "states": [{"key": "day"}]},
            "kind": "character",
            "slot": "day",
            "plate": {},
        },
        [{"reference_url": "https://a", "asset_id": "a", "name": "Hero"}],
        {"style": style.KEY, "aspectRatio": "1:1", "imageModel": "dola-seedream-5-0-pro"},
    )
    assert payload["aspect_ratio"] == "16:9" and payload["reference_urls"] == ["https://a"]
    assert payload["style_version"] == style.version() and payload["material_kind"] == "character"
    PlateBody.model_validate(payload)


@pytest.mark.asyncio
async def test_master_is_not_rewritten_by_legacy_image_writer(monkeypatch):
    async def forbidden(*a, **kw):
        raise AssertionError("Master should remain exact")

    monkeypatch.setattr(prompt_writer.adapt_mod, "ask_json", forbidden)
    text, by = await prompt_writer.write_image_prompt(
        "Approved master", kind="character", subject="A", design={"face": "B"}, style=style.KEY
    )
    assert text == "Approved master" and by == style.version()


@pytest.mark.asyncio
async def test_actual_seedream_request_gets_style_before_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(automation.avis_api, "is_configured", lambda: True)

    async def fetch(urls):
        return [b"identity", b"prop"]

    async def image(prompt, refs, **kw):
        calls.append((refs, kw))
        return [b"generated"]

    monkeypatch.setattr(automation, "_fetch_reference_bytes", fetch)
    monkeypatch.setattr(automation.avis_api, "generate_image_variants", image)
    monkeypatch.setattr(automation, "_publish", lambda data: "https://output")
    monkeypatch.setattr(automation, "_ingest_plate", lambda data: "media-1")
    await automation.generate_plate(
        "Approved",
        style=style.KEY,
        material_kind="character",
        style_version=style.version(),
        image_model="dola-seedream-5-0-pro",
        aspect_ratio="1:1",
        reference_urls=["https://a", "https://b"],
    )
    assert calls[0][0] == [style.reference_path("character").read_bytes(), b"identity", b"prop"]
    assert calls[0][1]["aspect_ratio"] == "16:9"

    async def missing(urls):
        return [b"prop"]

    monkeypatch.setattr(automation, "_fetch_reference_bytes", missing)
    with pytest.raises(automation.AutomationError, match="slots were not shifted"):
        await automation.generate_plate(
            "Approved",
            style=style.KEY,
            material_kind="character",
            image_model="dola-seedream-5-0-pro",
            reference_urls=["https://a", "https://b"],
        )
    assert len(calls) == 1


def test_material_style_revision_changes_run_input():
    from tests.test_production_run import fixture

    b = fixture()
    old = production_run.input_version(b)
    b["style"] = style.KEY
    assert production_run.input_version(b) != old
