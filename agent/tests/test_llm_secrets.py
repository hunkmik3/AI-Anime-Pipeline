"""Tests for the LLM secret-storage module.

Covers: file roundtrip, mode 0o600 enforcement, atomic writes, missing-file
empty-dict fallback, corruption recovery, defaults overlay for active
providers, key clearing.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from flowboard.services.llm import secrets


@pytest.fixture
def tmp_secrets_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the secrets module at a tmp file. Each test gets a fresh path
    so module-level state can't bleed across tests (the module reads
    FLOWBOARD_SECRETS_PATH on every call — no cached resolution)."""
    p = tmp_path / "secrets.json"
    monkeypatch.setenv("FLOWBOARD_SECRETS_PATH", str(p))
    return p


def test_read_returns_empty_when_file_missing(tmp_secrets_path: Path):
    assert not tmp_secrets_path.exists()
    assert secrets.read() == {}


def test_read_returns_empty_on_corrupt_file(tmp_secrets_path: Path):
    """Corrupt JSON must not crash the agent — return empty dict so
    callers' .get(...) chains still work."""
    tmp_secrets_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_secrets_path.write_text("{not json")
    assert secrets.read() == {}


def test_write_creates_parent_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """First-time setup — ~/.flowboard/ doesn't exist yet."""
    nested = tmp_path / "nested" / "subdir" / "secrets.json"
    monkeypatch.setenv("FLOWBOARD_SECRETS_PATH", str(nested))
    secrets.write({"apiKeys": {"openai": "sk-1"}})
    assert nested.exists()
    assert json.loads(nested.read_text()) == {"apiKeys": {"openai": "sk-1"}}


def test_write_sets_mode_0600(tmp_secrets_path: Path):
    """Critical — file must not be group/world readable. API keys live here."""
    secrets.write({"apiKeys": {"openai": "sk-secret"}})
    mode = stat.S_IMODE(os.stat(tmp_secrets_path).st_mode)
    assert mode == 0o600, f"expected 0o600 got {oct(mode)}"


def test_write_is_atomic_no_tmp_leftover(tmp_secrets_path: Path):
    """The temp file used for atomic-replace must not linger after success."""
    secrets.write({"apiKeys": {"openai": "sk-x"}})
    tmp = tmp_secrets_path.with_suffix(tmp_secrets_path.suffix + ".tmp")
    assert not tmp.exists()


def test_get_api_key_roundtrip(tmp_secrets_path: Path):
    secrets.set_api_key("openai", "sk-roundtrip")
    assert secrets.get_api_key("openai") == "sk-roundtrip"


def test_get_api_key_returns_none_when_missing(tmp_secrets_path: Path):
    """Missing key → None. Other providers' keys don't bleed through."""
    assert secrets.get_api_key("openai") is None
    # Intentionally use a non-shipped name to verify isolation — the
    # secrets layer doesn't validate keys against the registry.
    secrets.set_api_key("custom", "x-1")
    assert secrets.get_api_key("openai") is None


def test_get_api_key_returns_none_for_empty_string(tmp_secrets_path: Path):
    """Defensive — if a write somehow stored an empty string, treat as unset."""
    secrets.write({"apiKeys": {"openai": ""}})
    assert secrets.get_api_key("openai") is None


def test_set_api_key_clears_with_none(tmp_secrets_path: Path):
    """Clearing removes the entry entirely — not just empty-string."""
    secrets.set_api_key("openai", "sk-1")
    assert secrets.get_api_key("openai") == "sk-1"
    secrets.set_api_key("openai", None)
    assert secrets.get_api_key("openai") is None
    # Verify the entry is actually gone, not just shadowed
    doc = json.loads(tmp_secrets_path.read_text())
    assert "openai" not in (doc.get("apiKeys") or {})


def test_set_api_key_does_not_touch_other_providers(tmp_secrets_path: Path):
    """Editing one key must leave others alone — basic but worth a
    regression. Uses an unknown provider name as the "other" key to
    verify the secrets layer is provider-agnostic at this level."""
    secrets.set_api_key("openai", "sk-1")
    secrets.set_api_key("custom", "x-1")
    secrets.set_api_key("openai", "sk-2")
    assert secrets.get_api_key("custom") == "x-1"
    assert secrets.get_api_key("openai") == "sk-2"


def test_set_api_key_preserves_active_providers(tmp_secrets_path: Path):
    """Saving a key shouldn't wipe the user's feature routing."""
    secrets.set_feature_provider("vision", "gemini")
    secrets.set_api_key("openai", "sk-key")
    assert secrets.read_active_providers()["vision"] == "gemini"


def test_read_active_providers_empty_for_fresh_install(tmp_secrets_path: Path):
    """Brand-new install — no providers are pinned. Callers (registry,
    HTTP route) must handle missing keys; the forced-setup dialog in the
    UI is what nudges the user to set one up."""
    cfg = secrets.read_active_providers()
    assert cfg == {}


def test_read_active_providers_returns_only_saved(tmp_secrets_path: Path):
    """User picks Gemini for Vision; only that key is present. The other
    two features are absent (caller treats absence as "not configured")."""
    secrets.set_feature_provider("vision", "gemini")
    cfg = secrets.read_active_providers()
    assert cfg == {"vision": "gemini"}


def test_read_active_providers_ignores_garbage_values(tmp_secrets_path: Path):
    """Defensive — a hand-edited secrets.json with non-string values for a
    feature must drop the bad entry rather than crash. Missing slots are
    absent (no silent default substitution)."""
    secrets.write({
        "activeProviders": {
            "auto_prompt": "gemini",
            "vision": 42,        # bad — non-string
            "planner": None,     # bad — non-string
        }
    })
    cfg = secrets.read_active_providers()
    assert cfg == {"auto_prompt": "gemini"}


def test_is_active_providers_configured_false_when_empty(tmp_secrets_path: Path):
    assert secrets.is_active_providers_configured() is False


def test_is_active_providers_configured_false_when_partial(tmp_secrets_path: Path):
    secrets.set_feature_provider("vision", "gemini")
    secrets.set_feature_provider("planner", "gemini")
    # auto_prompt unset → not configured.
    assert secrets.is_active_providers_configured() is False


def test_is_active_providers_configured_false_when_mixed(tmp_secrets_path: Path):
    """All 3 set, but to different providers — single-provider invariant
    fails so the forced-setup gate prompts the user to consolidate."""
    secrets.set_feature_provider("auto_prompt", "claude")
    secrets.set_feature_provider("vision", "gemini")
    secrets.set_feature_provider("planner", "claude")
    assert secrets.is_active_providers_configured() is False


def test_is_active_providers_configured_true_when_all_match(tmp_secrets_path: Path):
    """All 3 set to the same provider — this is what the dialog's Apply
    button writes. Setup-complete state."""
    secrets.set_feature_provider("auto_prompt", "gemini")
    secrets.set_feature_provider("vision", "gemini")
    secrets.set_feature_provider("planner", "gemini")
    assert secrets.is_active_providers_configured() is True


def test_set_feature_provider_does_not_touch_api_keys(tmp_secrets_path: Path):
    """Switching a feature's provider shouldn't wipe configured API keys."""
    secrets.set_api_key("openai", "sk-keep")
    secrets.set_feature_provider("planner", "openai")
    assert secrets.get_api_key("openai") == "sk-keep"


def test_write_then_read_preserves_full_document(tmp_secrets_path: Path):
    """End-to-end: a populated document survives a write + read cycle."""
    secrets.set_api_key("openai", "sk-1")
    secrets.set_feature_provider("auto_prompt", "gemini")
    secrets.set_feature_provider("vision", "openai")

    raw = json.loads(tmp_secrets_path.read_text())
    assert raw["apiKeys"] == {"openai": "sk-1"}
    assert raw["activeProviders"]["auto_prompt"] == "gemini"
    assert raw["activeProviders"]["vision"] == "openai"


# ── env-first lookup (Phase 6.5) ────────────────────────────────────────


def test_get_api_key_env_var_wins_over_secrets_json(
    tmp_secrets_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``BYTEPLUS_KEY`` in the environment takes precedence over a
    Dreamina entry in secrets.json — .env is now the canonical store."""
    secrets.set_api_key("dreamina", "ark-from-secrets-json")
    monkeypatch.setenv("BYTEPLUS_KEY", "ark-from-env")
    assert secrets.get_api_key("dreamina") == "ark-from-env"


def test_get_api_key_falls_back_to_dreamina_api_key_env(
    tmp_secrets_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The Dreamina mapping accepts both ``BYTEPLUS_KEY`` and
    ``DREAMINA_API_KEY``. First non-empty wins (BYTEPLUS_KEY listed first)."""
    monkeypatch.setenv("DREAMINA_API_KEY", "ark-fallback")
    assert secrets.get_api_key("dreamina") == "ark-fallback"


def test_get_api_key_falls_back_to_secrets_json_when_env_unset(
    tmp_secrets_path: Path
):
    """No env vars → secrets.json entry serves the request."""
    secrets.set_api_key("dreamina", "ark-from-file")
    assert secrets.get_api_key("dreamina") == "ark-from-file"


def test_get_api_key_empty_env_var_falls_through(
    tmp_secrets_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Empty string env var must be treated as unset so a typo'd
    ``BYTEPLUS_KEY=`` line in .env doesn't shadow a valid secrets.json
    entry."""
    secrets.set_api_key("dreamina", "ark-from-file")
    monkeypatch.setenv("BYTEPLUS_KEY", "")
    monkeypatch.setenv("DREAMINA_API_KEY", "")
    assert secrets.get_api_key("dreamina") == "ark-from-file"


def test_get_api_key_unknown_provider_only_reads_secrets_json(
    tmp_secrets_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Providers without an env-var mapping (currently everything but
    dreamina) still resolve only via secrets.json — no accidental
    coupling to arbitrary env vars."""
    secrets.set_api_key("openai", "sk-from-file")
    # An env var with a similar name MUST NOT be read for openai.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert secrets.get_api_key("openai") == "sk-from-file"


def test_read_r2_config_env_wins_per_field(
    tmp_secrets_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Each R2 field is resolved independently — env-var first, then
    secrets.json. Lets a partial .env migration work (e.g. bucket lives
    in .env, secret rotated in secrets.json)."""
    secrets.set_r2_config(
        endpoint_url="https://file.r2.example.com",
        access_key_id="file-akid",
        secret_access_key="file-secret",
        bucket="file-bucket",
    )
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://env.r2.example.com")
    monkeypatch.setenv("R2_BUCKET", "env-bucket")
    cfg = secrets.read_r2_config()
    assert cfg["endpoint_url"] == "https://env.r2.example.com"
    assert cfg["bucket"] == "env-bucket"
    # Unset env fields fall back to secrets.json.
    assert cfg["access_key_id"] == "file-akid"
    assert cfg["secret_access_key"] == "file-secret"


def test_read_r2_config_env_only_works_without_secrets_json(
    tmp_secrets_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Phase 8 trajectory: secrets.json eventually goes away. Env-only
    config must populate ``read_r2_config()`` fully on its own."""
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://env.r2.example.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "env-akid")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "env-secret")
    monkeypatch.setenv("R2_BUCKET", "env-bucket")
    assert secrets.is_r2_configured() is True
    cfg = secrets.read_r2_config()
    assert cfg == {
        "endpoint_url": "https://env.r2.example.com",
        "access_key_id": "env-akid",
        "secret_access_key": "env-secret",
        "bucket": "env-bucket",
    }


def test_read_r2_config_public_base_url_env_optional(
    tmp_secrets_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``R2_PUBLIC_BASE_URL`` is the only optional field — when unset
    in BOTH env and secrets.json it's just absent from the result, not
    an empty string."""
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://env.r2.example.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "env-akid")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "env-secret")
    monkeypatch.setenv("R2_BUCKET", "env-bucket")
    cfg = secrets.read_r2_config()
    assert "public_base_url" not in cfg


def test_read_r2_config_empty_env_var_falls_through(
    tmp_secrets_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An empty-string env var (e.g. ``R2_BUCKET=``) must NOT shadow
    a valid secrets.json entry — same defensive rule as get_api_key."""
    secrets.set_r2_config(
        endpoint_url="https://file.r2.example.com",
        access_key_id="file-akid",
        secret_access_key="file-secret",
        bucket="file-bucket",
    )
    monkeypatch.setenv("R2_BUCKET", "")
    cfg = secrets.read_r2_config()
    assert cfg["bucket"] == "file-bucket"
