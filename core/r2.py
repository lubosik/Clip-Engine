"""
core/r2.py — Cloudflare R2 (S3-compatible) storage client.

All credentials come from core.settings — never hardcoded.  The client is
lazily created on first use so this module can be imported freely in tests
and config-only contexts without triggering network calls.

R2 is optional: when settings.r2_enabled is False every function raises a
RuntimeError rather than attempting a network call.  Check r2_enabled before
calling these functions if you want graceful fallback to local storage.

Key scheme (per REVAMP_CONTRACTS §3):
    campaigns/{campaign}/clips/{clip_id}.mp4
    campaigns/{campaign}/thumbs/{clip_id}.jpg
    campaigns/{campaign}/memes/{clip_id}.png
    campaigns/{campaign}/assets/{filename}
    campaigns/{campaign}/raw/{source_id}.mp4
    hero/{filename}
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Module-level boto3 client — created once, reused across calls.
_client: Any = None


def _get_client() -> Any:
    """Return (and lazily create) the boto3 S3 client for R2."""
    global _client
    if _client is not None:
        return _client

    import boto3  # type: ignore[import-untyped]

    from core.settings import get_settings

    s = get_settings()
    if not s.r2_enabled:
        raise RuntimeError(
            "R2 storage is not configured. Set R2_BUCKET, R2_ENDPOINT, "
            "R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY in your environment."
        )

    _client = boto3.client(
        "s3",
        endpoint_url=s.r2_endpoint,
        aws_access_key_id=s.r2_access_key_id,
        aws_secret_access_key=s.r2_secret_access_key,
        # R2 uses 'auto' as the region; boto3 requires a non-empty string
        region_name="auto",
    )
    return _client


def _bucket() -> str:
    from core.settings import get_settings

    return get_settings().r2_bucket  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_file(local_path: str | Path, key: str) -> None:
    """Upload a local file to R2 at *key*.

    Args:
        local_path: Absolute path to the local file.
        key:        Destination R2 object key.

    Raises:
        RuntimeError: if R2 is not configured.
        Exception:    propagates boto3 errors after logging.
    """
    client = _get_client()
    local_path = Path(local_path)
    size = local_path.stat().st_size
    try:
        client.upload_file(str(local_path), _bucket(), key)
        log.info("Uploaded to R2: key=%s size=%d", key, size)
    except Exception as exc:
        log.error("R2 upload_file failed: key=%s error=%s", key, exc)
        raise


def download_file(key: str, local_path: str | Path) -> None:
    """Download a file from R2 to *local_path*.

    Creates parent directories as needed.

    Args:
        key:        R2 object key.
        local_path: Destination path on the local filesystem.

    Raises:
        RuntimeError: if R2 is not configured.
        Exception:    propagates boto3 errors after logging.
    """
    client = _get_client()
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_file(_bucket(), key, str(local_path))
        log.info("Downloaded from R2: key=%s -> %s", key, local_path)
    except Exception as exc:
        log.error("R2 download_file failed: key=%s error=%s", key, exc)
        raise


def put_bytes(
    key: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> None:
    """Upload raw bytes to R2 at *key*.

    Useful for small objects (thumbnails, JSON metadata, test objects).

    Raises:
        RuntimeError: if R2 is not configured.
    """
    client = _get_client()
    try:
        client.put_object(
            Bucket=_bucket(),
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        log.info("Put bytes to R2: key=%s size=%d", key, len(data))
    except Exception as exc:
        log.error("R2 put_bytes failed: key=%s error=%s", key, exc)
        raise


def presign(key: str, expires: int = 3600) -> str:
    """Generate a presigned GET URL for *key*.

    Args:
        key:     R2 object key.
        expires: URL validity in seconds (default 1 hour).

    Returns:
        A presigned URL string.  The browser can fetch this URL directly
        without any credentials — it expires after *expires* seconds.

    Raises:
        RuntimeError: if R2 is not configured.
    """
    client = _get_client()
    try:
        url: str = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket(), "Key": key},
            ExpiresIn=expires,
        )
        return url
    except Exception as exc:
        log.error("R2 presign failed: key=%s error=%s", key, exc)
        raise


def exists(key: str) -> bool:
    """Return True if *key* exists in R2, False otherwise.

    Uses HeadObject — does not download the object body.

    Raises:
        RuntimeError: if R2 is not configured.
    """
    client = _get_client()
    try:
        client.head_object(Bucket=_bucket(), Key=key)
        return True
    except client.exceptions.NoSuchKey:
        return False
    except Exception as exc:
        # ClientError 404 → object not found; any other error → re-raise
        err_code = getattr(getattr(exc, "response", {}), "get", lambda *_: None)(
            "Error", {}
        ).get("Code", "")
        if err_code in ("404", "NoSuchKey"):
            return False
        log.error("R2 exists check failed: key=%s error=%s", key, exc)
        raise


def healthcheck() -> dict[str, Any]:
    """Verify R2 connectivity: write, read, delete a tiny test object.

    Returns:
        ``{"ok": True, "error": None}`` on success.
        ``{"ok": False, "error": "<message>"}`` on failure.
    """
    _TEST_KEY = "_healthcheck/clip-engine-probe.txt"
    _TEST_DATA = b"clip-engine-r2-healthcheck-ok"
    try:
        client = _get_client()
        # Write
        put_bytes(_TEST_KEY, _TEST_DATA, content_type="text/plain")
        # Read back
        response = client.get_object(Bucket=_bucket(), Key=_TEST_KEY)
        body = response["Body"].read()
        if body != _TEST_DATA:
            raise AssertionError(f"Healthcheck data mismatch: got {body!r}")
        # Delete
        client.delete_object(Bucket=_bucket(), Key=_TEST_KEY)
        log.info("R2 healthcheck passed")
        return {"ok": True, "error": None}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log.error("R2 healthcheck failed: %s", exc)
        return {"ok": False, "error": str(exc)}
