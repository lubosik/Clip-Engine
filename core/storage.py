"""
core/storage.py — filesystem paths under STORAGE_DIR.

Provides typed helpers for raw download paths, rendered clip paths,
and thumbnail paths. Creates subdirectories on first use.

STORAGE_DIR defaults to /data/clips (override via env var).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from core.settings import get_settings

log = logging.getLogger(__name__)


def _storage_root() -> Path:
    return Path(get_settings().storage_dir)


def raw_dir() -> Path:
    """Directory for raw source video downloads."""
    d = _storage_root() / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def clips_dir() -> Path:
    """Directory for rendered, watermarked final clips."""
    d = _storage_root() / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


def thumbs_dir() -> Path:
    """Directory for clip thumbnails."""
    d = _storage_root() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_dir(source_id: str) -> Path:
    """Ephemeral working directory for a single source's render pipeline."""
    # source_id may contain ":" — replace with "_" for filesystem safety
    safe_id = source_id.replace(":", "_").replace("/", "_")
    d = _storage_root() / "work" / safe_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def raw_path(source_id: str, ext: str = "mp4") -> Path:
    """Path for the raw download of a source video."""
    safe_id = source_id.replace(":", "_").replace("/", "_")
    return raw_dir() / f"{safe_id}.{ext}"


def clip_path(clip_id: int | str, ext: str = "mp4") -> Path:
    """Path for a final rendered clip."""
    return clips_dir() / f"clip_{clip_id}.{ext}"


def thumb_path(clip_id: int | str, ext: str = "jpg") -> Path:
    """Path for a clip thumbnail."""
    return thumbs_dir() / f"thumb_{clip_id}.{ext}"


def cleanup_raw(source_id: str) -> None:
    """Delete the raw download file for a source, if it exists."""
    for ext in ("mp4", "mkv", "webm", "mov"):
        p = raw_path(source_id, ext)
        if p.exists():
            try:
                p.unlink()
                log.info("Deleted raw file", extra={"path": str(p)})
            except OSError as exc:
                log.warning(
                    "Failed to delete raw file",
                    extra={"path": str(p), "error": str(exc)},
                )


def cleanup_work(source_id: str) -> None:
    """Remove the ephemeral work directory for a source."""
    d = work_dir(source_id)
    if d.exists():
        try:
            shutil.rmtree(d)
            log.info("Cleaned up work dir", extra={"path": str(d)})
        except OSError as exc:
            log.warning(
                "Failed to clean work dir",
                extra={"path": str(d), "error": str(exc)},
            )
