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

_PBKDF2_ITERATIONS = 600_000  # OWASP 2023 minimum for PBKDF2-HMAC-SHA256
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


def needs_rehash(stored: str) -> bool:
    """True when a stored hash uses an older scheme or fewer iterations than
    the current cost — the login path re-hashes transparently so existing
    accounts are upgraded to the stronger parameters on their next sign-in."""
    try:
        scheme, iters_s, _salt, _hash = stored.split("$")
        return scheme != f"pbkdf2_{_PBKDF2_ALGO}" or int(iters_s) < _PBKDF2_ITERATIONS
    except (ValueError, AttributeError):
        return True


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

TOKEN_TTL_SECONDS = int(os.getenv("FLOWBOARD_TOKEN_TTL_S", str(7 * 24 * 3600)))  # 7d


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(
    user_id: str, token_version: int = 0, *, ttl_seconds: int = TOKEN_TTL_SECONDS
) -> str:
    """Issue ``<payload>.<sig>`` where payload = {uid, tv, exp}. ``tv`` is the
    account's ``token_version`` at issue time; bumping it server-side revokes
    every outstanding token that still carries the old value."""
    payload = _b64u(
        json.dumps(
            {"uid": user_id, "tv": int(token_version), "exp": int(time.time()) + ttl_seconds}
        ).encode()
    )
    sig = _b64u(hmac.new(_server_secret(), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def decode_token(token: str) -> Optional[dict]:
    """Return the verified payload ``{uid, tv, exp}`` for a valid, unexpired,
    correctly-signed token; else None. Signature checked in constant time."""
    try:
        payload, sig = token.split(".", 1)
        expected = _b64u(hmac.new(_server_secret(), payload.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_b64u_decode(payload))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        uid = data.get("uid")
        if not isinstance(uid, str) or not uid:
            return None
        return {"uid": uid, "tv": int(data.get("tv", 0)), "exp": int(data.get("exp", 0))}
    except (ValueError, AttributeError, TypeError, json.JSONDecodeError):
        return None


def verify_token(token: str) -> Optional[str]:
    """Back-compat: just the user_id for a valid token (else None). New code
    should use ``decode_token`` to also read ``tv`` for revocation checks."""
    data = decode_token(token)
    return data["uid"] if data else None
