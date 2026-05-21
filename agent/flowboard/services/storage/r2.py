"""Cloudflare R2 implementation of the ``ObjectStorage`` Protocol.

R2 is S3-compatible: boto3 with a custom ``endpoint_url`` works
unchanged. The free tier (10 GB storage + 10M Class A ops / month) is
more than enough for a single-user prosumer workflow.

Setup is documented in ``docs/r2_setup.md``. Credentials live in
``~/.flowboard/secrets.json`` under ``r2``.

Why boto3 instead of a smaller dependency:

- We already need it for any S3-compatible bucket — swap target needs
  zero code change, just point ``endpoint_url`` somewhere else.
- aiobotocore would be more idiomatic in an async codebase, but the
  upload happens once per submit (one-shot, not per-frame), and the
  blocking call is wrapped in ``asyncio.to_thread`` at the call site.
- s3transfer (the upload helper) handles multipart for free; rolling
  our own is unnecessary risk.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from flowboard.services.llm import secrets

from .base import ObjectStorage, ObjectStorageError

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()
_DEFAULT: Optional["R2Storage"] = None


class R2Storage:
    """Cloudflare R2 client. S3-compatible.

    The boto3 client is constructed on first ``upload_and_presign`` call
    so test fixtures that don't touch storage never need boto3 in their
    sys.path. (boto3 IS in requirements.txt; this is just laziness for
    perf, not optionality.)
    """

    name = "r2"

    def __init__(
        self,
        *,
        endpoint_url: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        bucket: Optional[str] = None,
        public_base_url: Optional[str] = None,
    ) -> None:
        cfg = secrets.read_r2_config()
        self.endpoint_url = endpoint_url or cfg.get("endpoint_url") or ""
        self.access_key_id = access_key_id or cfg.get("access_key_id") or ""
        self.secret_access_key = secret_access_key or cfg.get("secret_access_key") or ""
        self.bucket = bucket or cfg.get("bucket") or ""
        self.public_base_url = (public_base_url or cfg.get("public_base_url") or "").rstrip("/")
        self._client = None  # lazy

    def is_configured(self) -> bool:
        return bool(
            self.endpoint_url
            and self.access_key_id
            and self.secret_access_key
            and self.bucket
        )

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore[import-not-found]
            from botocore.config import Config  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ObjectStorageError(
                "boto3 not installed — run `pip install -r agent/requirements.txt`"
            ) from exc
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            # R2 uses 'auto' region; signature v4 is required.
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
        return self._client

    def upload_and_presign(
        self,
        local_path: Path,
        *,
        key: str,
        content_type: Optional[str] = None,
        expires_seconds: int = 3600,
    ) -> str:
        if not local_path.exists():
            raise ObjectStorageError(f"local file does not exist: {local_path}")
        if not self.is_configured():
            raise ObjectStorageError("R2 not configured")

        client = self._get_client()
        extra: dict = {}
        if content_type:
            extra["ContentType"] = content_type
            # Long cache for content-addressed keys (filename = asset uuid):
            # the bytes never change for a given key, so browsers/CDNs
            # caching aggressively is desirable.
            extra["CacheControl"] = "public, max-age=86400"

        try:
            client.upload_file(
                str(local_path),
                self.bucket,
                key,
                ExtraArgs=extra or None,
            )
        except Exception as exc:  # noqa: BLE001 — boto3 raises a zoo of types
            logger.exception("r2: upload failed for key=%s", key)
            raise ObjectStorageError(f"R2 upload failed: {exc}") from exc

        # Public CDN passthrough — used when the bucket is fronted by a
        # public domain (e.g. cloudflare R2 public dev URL or a custom
        # domain). Skips presigning entirely; safe when the bucket
        # access is read-only-by-key (Cloudflare's default for the public
        # URL feature).
        if self.public_base_url:
            return f"{self.public_base_url}/{key}"

        try:
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("r2: presign failed for key=%s", key)
            raise ObjectStorageError(f"R2 presign failed: {exc}") from exc
        return url


def get_default_storage() -> R2Storage:
    """Process-wide singleton. Re-reads secrets each construction so a
    settings change picks up on next provider instantiation."""
    global _DEFAULT
    with _LOCK:
        if _DEFAULT is None:
            _DEFAULT = R2Storage()
        return _DEFAULT


def reset_default_for_tests() -> None:
    """Invalidate the cached singleton. Used by test fixtures that
    monkeypatch the secrets file mid-test."""
    global _DEFAULT
    with _LOCK:
        _DEFAULT = None
