"""Local secret storage for the multi-LLM provider layer.

Resolution order (env-first since Phase 6.5):

  1. Process environment (``os.environ``). The agent's main.py calls
     ``load_dotenv()`` at boot so ``.env`` in the repo root populates
     ``os.environ``. Twelve-factor compliant — CI / docker / shell
     injection can override the dev file without touching disk.
  2. ``~/.flowboard/secrets.json`` (legacy local store). Kept for back-
     compat with installs that pre-date the .env migration; planned
     deprecation in Phase 8.

Env var mapping:

  - Dreamina / Seedance API key   → ``BYTEPLUS_KEY`` or ``DREAMINA_API_KEY``
  - R2 endpoint                    → ``R2_ENDPOINT_URL``
  - R2 access key id               → ``R2_ACCESS_KEY_ID``
  - R2 secret access key           → ``R2_SECRET_ACCESS_KEY``
  - R2 bucket                      → ``R2_BUCKET``
  - R2 public CDN base (optional)  → ``R2_PUBLIC_BASE_URL``

Other API keys (openai, anthropic, ...) currently still read only from
secrets.json — extend the env mapping below if/when a new provider
needs it.

Schema of the legacy ``~/.flowboard/secrets.json``:

```json
{
  "apiKeys": {"openai": "sk-...", "dreamina": "ark-..."},
  "activeProviders": {
    "auto_prompt": "claude",
    "vision": "gemini",
    "planner": "claude"
  },
  "r2": {
    "endpoint_url": "https://<account>.r2.cloudflarestorage.com",
    "access_key_id": "...",
    "secret_access_key": "...",
    "bucket": "flowboard-media",
    "public_base_url": "https://media.example.com"  // optional CDN passthrough
  }
}
```

Stored as plain JSON with file mode ``0o600`` (owner read/write only).
Writes are atomic (``tmp + replace``).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_DEFAULT_PATH = Path.home() / ".flowboard" / "secrets.json"

# Provider → list of env var names accepted (first non-empty wins).
# Extend this map when a provider gets a canonical env var alongside
# its secrets.json entry.
_PROVIDER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "dreamina": ("BYTEPLUS_KEY", "DREAMINA_API_KEY"),
    "avis": ("AVIS_API_KEY",),
}


def _path() -> Path:
    """Indirection so tests can monkeypatch the location.

    Tests typically set ``FLOWBOARD_SECRETS_PATH`` to a tmp file. Production
    callers leave the env var unset and the default ``~/.flowboard/secrets.json``
    applies.
    """
    override = os.environ.get("FLOWBOARD_SECRETS_PATH")
    return Path(override) if override else _DEFAULT_PATH


def read() -> dict:
    """Load the full secrets document. Empty dict if file doesn't exist
    or is corrupt — callers must handle missing keys themselves."""
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("secrets: file unreadable, treating as empty (%s)", exc)
        return {}


def write(payload: dict) -> None:
    """Atomic write with mode 0o600. Creates parent dir if needed."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    # chmod BEFORE replace so the final file is never group/world-readable
    # even momentarily on filesystems that preserve permissions on rename.
    os.chmod(tmp, 0o600)
    tmp.replace(p)


# ── API key helpers ────────────────────────────────────────────────────

def get_api_key(provider: str) -> Optional[str]:
    """Resolve a provider API key, env-vars first then secrets.json.

    Returns ``None`` if no source supplies a non-empty value. The
    env-var mapping is in ``_PROVIDER_ENV_VARS``; providers without a
    canonical env var fall through to the legacy secrets.json store.
    """
    for env_name in _PROVIDER_ENV_VARS.get(provider, ()):
        val = os.environ.get(env_name)
        if isinstance(val, str) and val:
            return val
    doc = read()
    keys = doc.get("apiKeys") or {}
    val = keys.get(provider)
    return val if isinstance(val, str) and val else None


def set_api_key(provider: str, key: Optional[str]) -> None:
    """Set or clear (key=None) a provider's API key.

    Clearing removes the entry entirely so ``get_api_key`` returns None
    cleanly without falsy-empty-string ambiguity.
    """
    doc = read()
    keys = dict(doc.get("apiKeys") or {})
    if key is None or not key:
        keys.pop(provider, None)
    else:
        keys[provider] = key
    doc["apiKeys"] = keys
    write(doc)


# ── Active-providers helpers ───────────────────────────────────────────

# Features the UI configures. Order matters only for display; iteration
# order in this module is deterministic on Python 3.7+.
_FEATURES: tuple[str, ...] = ("auto_prompt", "vision", "planner")


def read_active_providers() -> dict[str, str]:
    """Return ``{feature: provider_name}`` for features the user has
    explicitly picked. No defaults — missing keys are absent.

    Callers must handle the missing case (a feature with no provider
    pinned can't dispatch). The HTTP layer surfaces this via the
    ``configured`` flag on ``GET /api/llm/config``; the dispatch layer
    raises ``LLMError`` so the user sees a clear "open settings" message
    instead of silently falling back to a provider they didn't pick.
    """
    doc = read()
    saved = doc.get("activeProviders") or {}
    if not isinstance(saved, dict):
        return {}
    return {k: v for k, v in saved.items() if isinstance(v, str) and v}


def is_active_providers_configured() -> bool:
    """True when the user has completed the AI Provider setup flow.

    Single-provider model: every feature must be pinned AND all three
    must point at the same provider. Mixed config (legacy hand-edits
    or older versions that allowed per-feature) returns False so the
    forced-setup gate prompts the user to consolidate.
    """
    saved = read_active_providers()
    if not all(f in saved for f in _FEATURES):
        return False
    values = {saved[f] for f in _FEATURES}
    return len(values) == 1


def set_feature_provider(feature: str, provider: str) -> None:
    """Pin one feature to one provider. Caller validates names."""
    doc = read()
    saved = dict(doc.get("activeProviders") or {})
    saved[feature] = provider
    doc["activeProviders"] = saved
    write(doc)


# ── R2 helpers ─────────────────────────────────────────────────────────

_R2_ENV_FIELDS: dict[str, str] = {
    "endpoint_url": "R2_ENDPOINT_URL",
    "access_key_id": "R2_ACCESS_KEY_ID",
    "secret_access_key": "R2_SECRET_ACCESS_KEY",
    "bucket": "R2_BUCKET",
    "public_base_url": "R2_PUBLIC_BASE_URL",
}


def read_r2_config() -> dict:
    """Return the R2 sub-document, env-vars first then secrets.json.

    Shape: ``{endpoint_url, access_key_id, secret_access_key, bucket,
    public_base_url?}``. Per-field resolution: each field independently
    prefers its env var (``R2_ENDPOINT_URL`` etc.) and falls back to
    secrets.json when unset — this lets a partial migration work (e.g.
    bucket in .env, secret rotated in secrets.json).

    Callers must validate completeness via ``is_r2_configured()``.
    Partial config is the same as no config and should surface a clear
    setup prompt rather than a runtime boto3 traceback.
    """
    doc = read()
    file_cfg = doc.get("r2") or {}
    if not isinstance(file_cfg, dict):
        file_cfg = {}
    merged: dict = {}
    for key, env_name in _R2_ENV_FIELDS.items():
        env_val = os.environ.get(env_name)
        if isinstance(env_val, str) and env_val:
            merged[key] = env_val
            continue
        file_val = file_cfg.get(key)
        if isinstance(file_val, str) and file_val:
            merged[key] = file_val
    return merged


def is_r2_configured() -> bool:
    cfg = read_r2_config()
    required = ("endpoint_url", "access_key_id", "secret_access_key", "bucket")
    return all(isinstance(cfg.get(k), str) and cfg.get(k) for k in required)


def set_r2_config(
    *,
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
    bucket: str,
    public_base_url: Optional[str] = None,
) -> None:
    doc = read()
    doc["r2"] = {
        "endpoint_url": endpoint_url,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "bucket": bucket,
        **({"public_base_url": public_base_url} if public_base_url else {}),
    }
    write(doc)
