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
import os
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


def _pot_provider_url() -> str | None:
    """URL of the bgutil PO-token provider sidecar, if configured.

    When BGUTIL_POT_URL is set (Railway: http://bgutil-pot.railway.internal:4416),
    yt-dlp mints YouTube proof-of-origin tokens through it — the maintained
    escalation for datacenter-IP bot-walls (docs: yt-dlp wiki PO-Token-Guide).
    Requires the bgutil-ytdlp-pot-provider pip plugin (in the Docker image).
    """
    return os.environ.get("BGUTIL_POT_URL") or None


def _apply_pot_provider(opts: dict, clients: list[str] | None) -> list[str] | None:
    """Wire the PO-token provider into a yt-dlp opts dict (no-op if unset).

    Returns the (possibly adjusted) client list: with a provider configured,
    the first attempt uses mweb — the client the POT approach is documented
    to work with — instead of bare default web.
    """
    pot_url = _pot_provider_url()
    if not pot_url:
        return clients
    ea = opts.setdefault("extractor_args", {})
    ea["youtubepot-bgutilhttp"] = {"base_url": [pot_url]}
    if clients is None:
        return ["mweb"]
    return clients


_COOKIES_TMP: Path | None = None


def _cookies_file() -> str | None:
    """Path to a YouTube cookies.txt, if the operator configured one.

    Escalation for hard IP walls that PO tokens don't clear (LOGIN_REQUIRED on
    every client — hit on Railway 2026-07-29). Two forms:
      YTDLP_COOKIES_FILE — path to a Netscape-format cookies.txt
      YTDLP_COOKIES_B64  — base64 of the cookies.txt content (Railway-friendly:
                           paste as an env var; decoded once per process to a
                           chmod-600 temp file)
    """
    global _COOKIES_TMP
    path = os.environ.get("YTDLP_COOKIES_FILE")
    if path:
        return path if Path(path).exists() else None
    b64 = os.environ.get("YTDLP_COOKIES_B64")
    if not b64:
        return None
    if _COOKIES_TMP is None or not _COOKIES_TMP.exists():
        import base64
        import tempfile

        try:
            data = base64.b64decode(b64)
        except Exception:
            log.warning("YTDLP_COOKIES_B64 is not valid base64; ignoring")
            return None
        fd, name = tempfile.mkstemp(prefix="yt_cookies_", suffix=".txt")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(name, 0o600)
        _COOKIES_TMP = Path(name)
    return str(_COOKIES_TMP)


def _apply_network_escalations(opts: dict) -> None:
    """Apply operator-configured cookies / proxy to a yt-dlp opts dict.

    YTDLP_PROXY — proxy URL for yt-dlp traffic only (e.g. a flat-rate static
    residential proxy: http://user:pass@host:port). Recommended over
    per-GB rotating proxies — video downloads are hundreds of MB.
    """
    ck = _cookies_file()
    if ck:
        opts.setdefault("cookiefile", ck)
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        opts.setdefault("proxy", proxy)


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
        clients = _apply_pot_provider(opts, clients)
        _apply_network_escalations(opts)
        if clients is not None:
            opts.setdefault("extractor_args", {})["youtube"] = {
                "player_client": clients
            }
            log.info(
                "YouTube operation with player_client=%s%s",
                ",".join(clients),
                " (PO-token provider active)" if _pot_provider_url() else "",
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
        # Resolve the SAME format selection the real download will use, so
        # bot-walls / format-unavailable surface here — before LLM ranking
        # spend — instead of at download time (2026-07-28 leak: probe passed,
        # ranking paid, download then failed on every source).
        "format": _YTDLP_FORMAT,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    log.info("Probing YouTube availability (no download)", extra={"url": url})
    try:
        _try_ytdlp_chain(base_opts, url, download=False)
    except Exception as exc:
        if _apify_downloader_available():
            # The paid Apify downloader can fetch what yt-dlp cannot from this
            # IP; don't kill the source on a probe bot-wall — the ranking spend
            # (~$0.02) is an acceptable bet against the fallback succeeding.
            log.warning(
                "probe failed (%s) but Apify downloader fallback is available; "
                "proceeding",
                str(exc)[:160],
                extra={"url": url},
            )
            return
        raise


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


_APIFY_DOWNLOADER_ACTOR = "api-ninja/youtube-video-downloader"


def _apify_downloader_available() -> bool:
    """The paid Apify downloader fallback is usable when a token is set."""
    return bool(os.environ.get("APIFY_TOKEN"))


def _download_youtube_via_apify(
    url: str, dest: Path, campaign: str | None = None
) -> Path:
    """Download a YouTube video through the api-ninja Apify actor.

    Operator-chosen reliable path (2026-07-29) for when yt-dlp is bot-walled
    on the host IP. format=1080, ttl=none → temp downloadUrl (~15 min window),
    streamed straight to `dest`. Spend lands in the apify_runs ledger
    (kind='download'). Validated live: run SUCCEEDED, downloadUrl HTTP 200.
    """
    from core.apify import Apify

    apify = Apify()
    items = apify.run(
        _APIFY_DOWNLOADER_ACTOR,
        {"urls": [url], "format": "1080", "ttl": "none"},
        campaign=campaign,
        kind="download",
    )
    item = next(
        (i for i in items if i.get("status") == "completed" and i.get("downloadUrl")),
        None,
    )
    if item is None:
        raise RuntimeError(
            f"Apify downloader returned no completed item for {url}: "
            f"{[{k: i.get(k) for k in ('status', 'error')} for i in items]}"
        )
    out = dest.with_suffix(".mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    log.info(
        "Apify downloader: streaming %sMB from temp URL",
        item.get("fileSizeMB"),
        extra={"url": url},
    )
    with httpx.stream(
        "GET", item["downloadUrl"], timeout=httpx.Timeout(30.0, read=600.0),
        follow_redirects=True,
    ) as resp:
        resp.raise_for_status()
        with out.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
    if out.stat().st_size < 1024:
        raise RuntimeError(f"Apify downloader produced a suspiciously small file for {url}")
    return out


def download_source(
    source_id: str,
    platform: str,
    url: str,
    raw: dict,
    campaign: str | None = None,
) -> Path:
    """
    Download a source video to STORAGE_DIR/raw/.

    Args:
        source_id: e.g. "youtube:abc123"
        platform:  "youtube" | "tiktok" | "instagram"
        url:       canonical source URL
        raw:       original discovery item dict (may contain direct videoUrl)
        campaign:  optional campaign name for the Apify spend ledger

    Returns:
        Path to the downloaded file.

    Raises:
        RuntimeError if download fails or no suitable URL is found.
    """
    dest = raw_path(source_id)  # will be adjusted for actual extension

    if platform == "youtube":
        try:
            return _download_youtube(url, dest)
        except Exception as exc:
            if not _apify_downloader_available():
                raise
            log.warning(
                "yt-dlp download failed (%s); falling back to Apify downloader",
                str(exc)[:160],
                extra={"url": url},
            )
            return _download_youtube_via_apify(url, dest, campaign=campaign)

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
