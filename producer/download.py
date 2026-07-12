"""
producer/download.py — raw source video downloads.

YouTube: yt-dlp (capped at 1080p, mp4 container)
TikTok/Instagram: httpx download from videoUrl / downloadedVideo field

yt-dlp is imported lazily so that importing this module does not fail
in test environments where it is not installed.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from core.storage import cleanup_raw, raw_path

log = logging.getLogger(__name__)

# yt-dlp format: best mp4 <= 1080p; fallback to best available
_YTDLP_FORMAT = (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
    "bestvideo[height<=1080]+bestaudio/"
    "best[height<=1080][ext=mp4]/"
    "best"
)


# Player-client retry chain for YouTube's "Sign in to confirm you're not a bot"
# check, which targets datacenter IPs (Railway) on the default web client. The
# ios/tv/android innertube clients are checked far less aggressively, so a
# blocked download often succeeds on retry with a different client.
_YTDLP_CLIENT_CHAIN: list[list[str] | None] = [
    None,                    # default (web) — works for many videos
    ["ios", "tv"],           # most reliable bypass pair from datacenter IPs
    ["android"],             # last resort
]


def _try_ytdlp_chain(
    base_opts: dict,
    url: str,
    *,
    download: bool,
) -> Any:
    """Execute a yt-dlp operation through the _YTDLP_CLIENT_CHAIN retry loop.

    download=True:  calls ydl.download([url]) — returns None on success.
    download=False: calls ydl.extract_info(url, download=False) — returns the
                    info dict on success (used by probe_youtube).

    Raises on unrecoverable errors or after all chain entries are exhausted.
    Only bot-check / DRM / format-unavailable errors trigger a client retry;
    everything else (404, private, network) propagates immediately.
    """
    try:
        import yt_dlp  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "yt-dlp is required for YouTube operations. "
            "Install it with: pip install yt-dlp"
        ) from exc

    last_exc: Exception | None = None
    for clients in _YTDLP_CLIENT_CHAIN:
        opts = dict(base_opts)
        if clients is not None:
            opts["extractor_args"] = {"youtube": {"player_client": clients}}
            log.info(
                "Retrying YouTube operation with player_client=%s",
                ",".join(clients),
                extra={"url": url},
            )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                if download:
                    ydl.download([url])
                    return None
                else:
                    return ydl.extract_info(url, download=False)
            last_exc = None  # unreachable but keeps the loop logic clear
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            # Only retry for bot-check / DRM / format issues; other errors
            # (404, private video, network) fail the same way on every client.
            retryable = (
                "Sign in to confirm" in msg
                or "not a bot" in msg
                or "DRM protected" in msg
                or "Requested format is not available" in msg
            )
            if not retryable:
                raise

    if last_exc is not None:
        raise last_exc
    return None


def probe_youtube(url: str) -> None:
    """Probe a YouTube URL for availability WITHOUT downloading.

    Uses the same _YTDLP_CLIENT_CHAIN retry logic as _download_youtube.
    Call this BEFORE LLM ranking to avoid paying for clips that cannot
    be downloaded (cost guard).

    Raises on failure (DRM, private video, bot-check, format unavailable).
    """
    base_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    log.info("Probing YouTube availability (no download)", extra={"url": url})
    _try_ytdlp_chain(base_opts, url, download=False)


def _download_youtube(url: str, dest: Path) -> Path:
    base_opts: dict[str, Any] = {
        "format": _YTDLP_FORMAT,
        "outtmpl": str(dest.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    log.info("Downloading YouTube video", extra={"url": url, "dest": str(dest)})
    _try_ytdlp_chain(base_opts, url, download=True)

    # yt-dlp may change the extension; find the actual file
    mp4_path = dest.with_suffix(".mp4")
    if mp4_path.exists():
        return mp4_path

    # Fallback: search for any video file with the same stem
    for candidate in dest.parent.glob(f"{dest.stem}.*"):
        if candidate.suffix in {".mp4", ".mkv", ".webm", ".mov"}:
            return candidate

    raise FileNotFoundError(f"yt-dlp completed but output file not found near: {dest}")


def _download_direct_url(video_url: str, dest: Path) -> Path:
    """Download a direct video URL (TikTok/Instagram CDN) via httpx."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; Pixel 5) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
    }

    log.info("Downloading direct video URL", extra={"url": video_url, "dest": str(dest)})

    try:
        with httpx.stream("GET", video_url, headers=headers, follow_redirects=True, timeout=120) as resp:
            resp.raise_for_status()

            # Infer extension from content-type
            content_type = resp.headers.get("content-type", "")
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".mp4"
            if ext == ".bin":
                ext = ".mp4"

            actual_dest = dest.with_suffix(ext)
            actual_dest.parent.mkdir(parents=True, exist_ok=True)

            with actual_dest.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 64):
                    f.write(chunk)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"HTTP {exc.response.status_code} downloading {video_url}"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"Network error downloading {video_url}: {exc}") from exc

    log.info(
        "Download complete",
        extra={"dest": str(actual_dest), "size_mb": round(actual_dest.stat().st_size / 1_048_576, 2)},
    )
    return actual_dest


def _get_tiktok_video_url(raw: dict) -> str | None:
    """Extract the best available download URL from a TikTok discovery item."""
    for key in ("downloadedVideo", "videoUrl", "webVideoUrl", "videoWebUrl"):
        val = raw.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            return val
    return None


def _get_instagram_video_url(raw: dict) -> str | None:
    """Extract the best available download URL from an Instagram discovery item."""
    for key in ("videoUrl", "downloadUrl", "url"):
        val = raw.get(key)
        if val and isinstance(val, str) and val.startswith("http") and "video" in val:
            return val
    # Fallback: any URL field
    for key in ("videoUrl", "url"):
        val = raw.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            return val
    return None


def download_source(
    source_id: str,
    platform: str,
    url: str,
    raw: dict,
) -> Path:
    """
    Download a source video to STORAGE_DIR/raw/.

    Args:
        source_id: e.g. "youtube:abc123"
        platform:  "youtube" | "tiktok" | "instagram"
        url:       canonical source URL
        raw:       original discovery item dict (may contain direct videoUrl)

    Returns:
        Path to the downloaded file.

    Raises:
        RuntimeError if download fails or no suitable URL is found.
    """
    dest = raw_path(source_id)  # will be adjusted for actual extension

    if platform == "youtube":
        return _download_youtube(url, dest)

    elif platform == "tiktok":
        video_url = _get_tiktok_video_url(raw)
        if not video_url:
            raise RuntimeError(
                f"No downloadable video URL found for TikTok source {source_id}. "
                f"Available raw keys: {list(raw)}"
            )
        return _download_direct_url(video_url, dest)

    elif platform == "instagram":
        video_url = _get_instagram_video_url(raw)
        if not video_url:
            raise RuntimeError(
                f"No downloadable video URL found for Instagram source {source_id}. "
                f"Available raw keys: {list(raw)}"
            )
        return _download_direct_url(video_url, dest)

    else:
        raise ValueError(f"Unknown platform: {platform!r}")


def cleanup_source(source_id: str) -> None:
    """Remove raw download file for a source after rendering is complete."""
    cleanup_raw(source_id)
