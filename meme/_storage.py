"""
meme/_storage.py — local storage helpers for meme images.

Creates a `memes/` subdirectory under STORAGE_DIR (mirrors core/storage.py
conventions without modifying that file).

Also provides a unified save_meme_image() that routes to R2 when enabled,
else writes to local storage and returns the appropriate path string.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def meme_local_dir() -> Path:
    """Return (and create) the local meme image storage directory."""
    from core.settings import get_settings

    d = Path(get_settings().storage_dir) / "memes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def meme_local_path(clip_id: int | str) -> Path:
    """Local filesystem path for a meme image, keyed by clip_id."""
    return meme_local_dir() / f"meme_{clip_id}.png"


def save_meme_image(clip_id: int | str, campaign: str, image_bytes: bytes) -> str:
    """
    Persist meme image bytes and return the stored path string.

    R2 mode: uploads to campaigns/{campaign}/memes/{clip_id}.png and returns
             the r2:// reference.
    Local mode: writes to {STORAGE_DIR}/memes/meme_{clip_id}.png and returns
                the absolute path string.

    Args:
        clip_id:     Clip row id (used as filename component).
        campaign:    Campaign name (used in R2 key).
        image_bytes: Raw PNG bytes.

    Returns:
        Path string suitable for Clip.file_path.
    """
    from core.settings import get_settings
    from core.storage import r2_key_for_meme

    s = get_settings()

    if s.r2_enabled:
        from core import r2

        key = r2_key_for_meme(campaign, clip_id)
        r2.put_bytes(key, image_bytes, content_type="image/png")
        log.info("Meme image saved to R2: key=%s size=%d", key, len(image_bytes))
        return f"r2://{key}"
    else:
        local = meme_local_path(clip_id)
        local.write_bytes(image_bytes)
        log.info(
            "Meme image saved locally: path=%s size=%d", local, len(image_bytes)
        )
        return str(local)
