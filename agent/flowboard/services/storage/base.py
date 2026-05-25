"""Storage Protocol + the public ``prepare_image_url`` entry point.

Kept tiny so swapping R2 for MinIO/S3/Backblaze is a 1-file change.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


class ObjectStorageError(RuntimeError):
    """Raised on upload / presign failures.

    The provider layer catches this and re-raises as
    ``VideoError("bad_input")`` so the worker surfaces a consistent
    error vocab. Don't add a code field here — callers translate.
    """


@runtime_checkable
class ObjectStorage(Protocol):
    """Anything that can host local bytes at a public HTTPS URL."""

    name: str

    def is_configured(self) -> bool:
        """Cheap check: are credentials + bucket set? No network call."""
        ...

    def upload_and_presign(
        self,
        local_path: Path,
        *,
        key: str,
        content_type: Optional[str] = None,
        expires_seconds: int = 3600,
    ) -> str:
        """Upload ``local_path`` to ``key`` and return a presigned GET URL.

        - ``key`` is the object key inside the bucket (e.g.
          ``media/<project_id>/<asset_id>.png``).
        - Implementations MUST be idempotent: re-uploading the same bytes
          for the same key is allowed and overwrites cleanly.
        - Expiry default 1h matches Dreamina's 5-10 min generation window
          with a 10x safety buffer.
        """
        ...


def prepare_image_url(
    local_path: Path,
    *,
    project_id: Optional[str] = None,
    asset_id: Optional[str] = None,
    storage: Optional[ObjectStorage] = None,
) -> str:
    """Hoist a local media file to a public HTTPS URL.

    Used by ``DreaminaVideoProvider`` and any future provider whose
    upstream API expects ``image_url.url`` to be reachable from the
    public internet.

    Key shape: ``media/<project_id>/<asset_id>.<ext>`` when both are
    given, else ``media/anonymous/<filename>``. Mirrors the
    ``storage/media/`` local layout for easy bucket inspection.
    """
    from .r2 import get_default_storage  # avoid circular import at module load

    store = storage or get_default_storage()
    if not store.is_configured():
        raise ObjectStorageError(
            "object storage is not configured — see docs/r2_setup.md "
            "and populate ~/.flowboard/secrets.json `r2` block"
        )

    ext = local_path.suffix.lstrip(".") or "bin"
    if project_id and asset_id:
        key = f"media/{project_id}/{asset_id}.{ext}"
    else:
        key = f"media/anonymous/{local_path.name}"

    content_type = _guess_content_type(ext)
    return store.upload_and_presign(
        local_path,
        key=key,
        content_type=content_type,
        expires_seconds=3600,
    )


_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "mp4": "video/mp4",
    # Audio — for Seedance 2.0 reference_audio hosting (Phase 7). Same
    # public-URL requirement as images; the bucket serves these alongside
    # image refs under media/<project>/<asset>.<ext>.
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
}


def _guess_content_type(ext: str) -> Optional[str]:
    return _CONTENT_TYPES.get(ext.lower())
