"""App authentication primitives — password hashing + signed session tokens.

Stdlib-only (no extra deps): PBKDF2-HMAC-SHA256 for passwords and a compact
HMAC-signed token (mini-JWT) for sessions. Used by the account/login routes
and the ``current_user`` dependency.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as _secrets
import time
from typing import Optional

from flowboard.config import STORAGE_DIR

# ── password hashing ────────────────────────────────────────────────────

_PBKDF2_ITERATIONS = 200_000
_PBKDF2_ALGO = "sha256"


def hash_password(password: str) -> str:
    """Return ``pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>``."""
    salt = _secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a ``hash_password`` string."""
    try:
        scheme, iters_s, salt_hex, hash_hex = stored.split("$")
        if scheme != f"pbkdf2_{_PBKDF2_ALGO}":
            return False
        dk = hashlib.pbkdf2_hmac(
            _PBKDF2_ALGO, password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ── server secret (stable across restarts) ──────────────────────────────

_secret_cache: Optional[bytes] = None


def _server_secret() -> bytes:
    """The HMAC key for signing tokens. ``FLOWBOARD_SECRET_KEY`` wins; else a
    random key is generated once and persisted under STORAGE_DIR so tokens
    survive restarts without forcing config."""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    env = os.getenv("FLOWBOARD_SECRET_KEY")
    if env:
        _secret_cache = env.encode("utf-8")
        return _secret_cache
    key_path = STORAGE_DIR / "secret.key"
    if key_path.exists():
        _secret_cache = key_path.read_bytes()
        return _secret_cache
    key = _secrets.token_bytes(32)
    try:
        key_path.write_bytes(key)
        os.chmod(key_path, 0o600)
    except OSError:
        pass  # in-memory fallback — tokens won't survive restart, but boot works
    _secret_cache = key
    return key


# ── session tokens (HMAC-signed, expiring) ───────────────────────────────

TOKEN_TTL_SECONDS = int(os.getenv("FLOWBOARD_TOKEN_TTL_S", str(30 * 24 * 3600)))  # 30d


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(user_id: str, *, ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    """Issue ``<payload>.<sig>`` where payload = {uid, exp}."""
    payload = _b64u(json.dumps({"uid": user_id, "exp": int(time.time()) + ttl_seconds}).encode())
    sig = _b64u(hmac.new(_server_secret(), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_token(token: str) -> Optional[str]:
    """Return the user_id for a valid, unexpired token; else None."""
    try:
        payload, sig = token.split(".", 1)
        expected = _b64u(hmac.new(_server_secret(), payload.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_b64u_decode(payload))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        uid = data.get("uid")
        return uid if isinstance(uid, str) and uid else None
    except (ValueError, AttributeError, json.JSONDecodeError):
        return None
