"""Object storage abstraction used by image-hosting paths.

Dreamina (and other providers that consume images via ``image_url.url``)
need a public HTTPS endpoint for each reference image. The local
``/media/{id}`` route serves the bytes Flow-internally but isn't
reachable from BytePlus's servers, so we mirror to an S3-compatible
bucket (Cloudflare R2 by default).

Public surface:

- ``prepare_image_url(local_path, *, project_id=None, asset_id=None) -> str``
  Idempotent: returns a freshly-signed URL each call (1h expiry, matches
  Dreamina's 5-10 min generation envelope).
- ``ObjectStorage`` Protocol — swap in S3 / MinIO / Backblaze by
  implementing the same shape and registering it.
"""
from __future__ import annotations

from .base import ObjectStorage, ObjectStorageError, prepare_image_url
from .r2 import R2Storage, get_default_storage, reset_default_for_tests

__all__ = [
    "ObjectStorage",
    "ObjectStorageError",
    "R2Storage",
    "get_default_storage",
    "prepare_image_url",
    "reset_default_for_tests",
]
