"""
producer/discover.py — multi-platform video discovery via Apify actors.

Public interface:
    discover_youtube(cfg, apify) -> list[dict]
    discover_tiktok(cfg, apify) -> list[dict]
    discover_instagram(cfg, apify) -> list[dict]
    discover_all(campaign_cfg, apify) -> list[dict]

All functions return candidates normalised to a common dict:
    {
        platform: str,
        native_id: str,
        source_id: str,   # f"{platform}:{native_id}"
        url: str,
        title: str | None,
        author_handle: str | None,
        view_count: int,
        duration_sec: float | None,
        published_at: str | None,
        raw: dict,        # original actor item
    }
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from producer.dedupe import compute_source_id

if TYPE_CHECKING:
    from core.apify import Apify
    from core.config import CampaignConfig

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Actor IDs (per SPEC §3)
# ---------------------------------------------------------------------------

ACTOR_YT_SCRAPER = "streamers/youtube-scraper"
ACTOR_TIKTOK_SCRAPER = "clockworks/free-tiktok-scraper"
ACTOR_IG_REEL_SCRAPER = "apify/instagram-reel-scraper"
ACTOR_IG_SCRAPER = "apify/instagram-scraper"

_UPLOADED_WITHIN_MAP = {
    "hour": "hour",
    "day": "today",
    "week": "week",
    "month": "month",
    "year": "year",
}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _norm_youtube(item: dict[str, Any]) -> dict[str, Any] | None:
    native_id = item.get("id") or item.get("videoId")
    url = item.get("url") or item.get("watchUrl")
    if not native_id or not url:
        log.warning("YouTube item missing id/url; skipping", extra={"item_keys": list(item)})
        return None
    return {
        "platform": "youtube",
        "native_id": str(native_id),
        "source_id": compute_source_id("youtube", str(native_id)),
        "url": url,
        "title": item.get("title"),
        "author_handle": item.get("channelName") or item.get("channelHandle"),
        "view_count": int(item.get("viewCount") or 0),
        "duration_sec": _parse_yt_duration(item.get("duration")),
        "published_at": item.get("date"),
        "raw": item,
    }


def _parse_yt_duration(d: Any) -> float | None:
    """Parse YouTube duration string 'HH:MM:SS' or 'MM:SS' to seconds."""
    if d is None:
        return None
    if isinstance(d, (int, float)):
        return float(d)
    parts = str(d).split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return None


def _norm_tiktok(item: dict[str, Any]) -> dict[str, Any] | None:
    native_id = item.get("id") or item.get("videoId")
    url = (
        item.get("webVideoUrl")
        or item.get("videoWebUrl")
        or item.get("shareUrl")
        or item.get("url")
    )
    if not native_id or not url:
        log.warning("TikTok item missing id/url; skipping", extra={"item_keys": list(item)})
        return None
    return {
        "platform": "tiktok",
        "native_id": str(native_id),
        "source_id": compute_source_id("tiktok", str(native_id)),
        "url": url,
        "title": item.get("text") or item.get("title"),
        "author_handle": (
            item.get("authorMeta", {}).get("name")
            if isinstance(item.get("authorMeta"), dict)
            else item.get("authorHandle")
        ),
        "view_count": int(item.get("playCount") or item.get("videoPlayCount") or 0),
        "duration_sec": float(item.get("videoMeta", {}).get("duration") or item.get("duration") or 0) or None,
        "published_at": str(item.get("createTimeISO") or item.get("createTime") or ""),
        "raw": item,
    }


def _norm_instagram(item: dict[str, Any]) -> dict[str, Any] | None:
    native_id = (
        item.get("id")
        or item.get("shortCode")
        or item.get("shortcode")
    )
    url = (
        item.get("url")
        or item.get("videoUrl")
        or item.get("displayUrl")
    )
    if not native_id or not url:
        log.warning("Instagram item missing id/url; skipping", extra={"item_keys": list(item)})
        return None
    return {
        "platform": "instagram",
        "native_id": str(native_id),
        "source_id": compute_source_id("instagram", str(native_id)),
        "url": item.get("url") or url,
        "title": item.get("caption") or item.get("title"),
        "author_handle": item.get("ownerUsername") or item.get("username"),
        "view_count": int(item.get("videoViewCount") or item.get("videoPlayCount") or 0),
        "duration_sec": float(item.get("videoDuration") or 0) or None,
        "published_at": str(item.get("timestamp") or item.get("takenAtTimestamp") or ""),
        "raw": item,
    }


# ---------------------------------------------------------------------------
# Per-platform discovery
# ---------------------------------------------------------------------------

def discover_youtube(
    cfg: "CampaignConfig",
    apify: "Apify",
) -> list[dict]:
    """Discover YouTube videos via streamers/youtube-scraper."""
    yt_cfg = cfg.sources.youtube
    if not yt_cfg:
        return []

    all_candidates: list[dict] = []

    # Search by terms
    for term in yt_cfg.search_terms:
        run_input: dict[str, Any] = {
            "searchQueries": [term],
            "maxResultsShorts": 0,
            "maxResults": 20,
        }
        if yt_cfg.min_view_count:
            run_input["minViewCount"] = yt_cfg.min_view_count
        upload_period = _UPLOADED_WITHIN_MAP.get(yt_cfg.uploaded_within, "year")
        run_input["uploadDate"] = upload_period

        log.info("YouTube search", extra={"term": term, "campaign": cfg.name})
        try:
            items = apify.run(ACTOR_YT_SCRAPER, run_input)
        except Exception as exc:
            log.error(
                "YouTube discovery failed for search term",
                extra={"term": term, "error": str(exc)},
            )
            continue

        for item in items:
            normed = _norm_youtube(item)
            if normed:
                # Post-filter by view count (actor may not always honour the param)
                if normed["view_count"] < yt_cfg.min_view_count:
                    continue
                all_candidates.append(normed)

    # Explicit channel URLs
    for channel_url in yt_cfg.channels:
        run_input = {
            "startUrls": [{"url": channel_url}],
            "maxResultsShorts": 0,
            "maxResults": 20,
        }
        if yt_cfg.min_view_count:
            run_input["minViewCount"] = yt_cfg.min_view_count

        log.info("YouTube channel scrape", extra={"channel": channel_url, "campaign": cfg.name})
        try:
            items = apify.run(ACTOR_YT_SCRAPER, run_input)
        except Exception as exc:
            log.error(
                "YouTube discovery failed for channel",
                extra={"channel": channel_url, "error": str(exc)},
            )
            continue

        for item in items:
            normed = _norm_youtube(item)
            if normed and normed["view_count"] >= yt_cfg.min_view_count:
                all_candidates.append(normed)

    # Dedupe within this discovery call by source_id
    seen: set[str] = set()
    unique = []
    for c in all_candidates:
        if c["source_id"] not in seen:
            seen.add(c["source_id"])
            unique.append(c)

    log.info(
        "YouTube discovery complete",
        extra={"campaign": cfg.name, "candidates": len(unique)},
    )
    return unique


def discover_tiktok(
    cfg: "CampaignConfig",
    apify: "Apify",
) -> list[dict]:
    """Discover TikTok videos via clockworks/free-tiktok-scraper."""
    tt_cfg = cfg.sources.tiktok
    if not tt_cfg:
        return []

    all_candidates: list[dict] = []

    # Profiles
    for profile in tt_cfg.profiles:
        run_input: dict[str, Any] = {
            "profiles": [profile],
            "resultsPerPage": 20,
        }
        log.info("TikTok profile scrape", extra={"profile": profile, "campaign": cfg.name})
        try:
            items = apify.run(ACTOR_TIKTOK_SCRAPER, run_input)
        except Exception as exc:
            log.error(
                "TikTok discovery failed for profile",
                extra={"profile": profile, "error": str(exc)},
            )
            continue
        for item in items:
            normed = _norm_tiktok(item)
            if normed:
                all_candidates.append(normed)

    # Hashtags
    for hashtag in tt_cfg.hashtags:
        run_input = {
            "hashtags": [hashtag],
            "resultsPerPage": 20,
        }
        log.info("TikTok hashtag scrape", extra={"hashtag": hashtag, "campaign": cfg.name})
        try:
            items = apify.run(ACTOR_TIKTOK_SCRAPER, run_input)
        except Exception as exc:
            log.error(
                "TikTok discovery failed for hashtag",
                extra={"hashtag": hashtag, "error": str(exc)},
            )
            continue
        for item in items:
            normed = _norm_tiktok(item)
            if normed:
                all_candidates.append(normed)

    seen: set[str] = set()
    unique = []
    for c in all_candidates:
        if c["source_id"] not in seen:
            seen.add(c["source_id"])
            unique.append(c)

    log.info(
        "TikTok discovery complete",
        extra={"campaign": cfg.name, "candidates": len(unique)},
    )
    return unique


def discover_instagram(
    cfg: "CampaignConfig",
    apify: "Apify",
) -> list[dict]:
    """
    Discover Instagram Reels via apify/instagram-scraper +
    apify/instagram-reel-scraper.
    """
    ig_cfg = cfg.sources.instagram
    if not ig_cfg:
        return []

    all_candidates: list[dict] = []

    for profile in ig_cfg.profiles:
        # Recent reels via instagram-scraper
        run_input: dict[str, Any] = {
            "usernames": [profile],
            "resultsType": "reels",
            "onlyPostsNewerThan": "1 week",
            "resultsLimit": 20,
        }
        log.info(
            "Instagram scraper (recent reels)",
            extra={"profile": profile, "campaign": cfg.name},
        )
        try:
            items = apify.run(ACTOR_IG_SCRAPER, run_input)
        except Exception as exc:
            log.error(
                "Instagram scraper failed",
                extra={"profile": profile, "error": str(exc)},
            )
            items = []

        for item in items:
            normed = _norm_instagram(item)
            if normed:
                all_candidates.append(normed)

        # Reel scraper for richer metadata (videoUrl, transcript)
        reel_run_input: dict[str, Any] = {
            "directUrls": [f"https://www.instagram.com/{profile}/reels/"],
            "resultsLimit": 20,
        }
        log.info(
            "Instagram reel scraper",
            extra={"profile": profile, "campaign": cfg.name},
        )
        try:
            reel_items = apify.run(ACTOR_IG_REEL_SCRAPER, reel_run_input)
        except Exception as exc:
            log.error(
                "Instagram reel scraper failed",
                extra={"profile": profile, "error": str(exc)},
            )
            reel_items = []

        for item in reel_items:
            normed = _norm_instagram(item)
            if normed:
                all_candidates.append(normed)

    seen: set[str] = set()
    unique = []
    for c in all_candidates:
        if c["source_id"] not in seen:
            seen.add(c["source_id"])
            unique.append(c)

    log.info(
        "Instagram discovery complete",
        extra={"campaign": cfg.name, "candidates": len(unique)},
    )
    return unique


def _is_excluded_by_keywords(
    candidate: dict[str, Any],
    exclude_keywords: list[str],
) -> bool:
    """Return True if the candidate title/caption matches any excluded keyword.

    Comparison is case-insensitive substring match.
    """
    if not exclude_keywords:
        return False
    title = (candidate.get("title") or "").lower()
    for kw in exclude_keywords:
        if kw.lower() in title:
            log.debug(
                "Candidate excluded by keyword filter",
                extra={"source_id": candidate.get("source_id"), "keyword": kw},
            )
            return True
    return False


def discover_all(
    campaign_cfg: "CampaignConfig",
    apify: "Apify",
) -> list[dict]:
    """
    Run all configured platforms, apply exclusion filters, and return the
    combined deduplicated candidate list.

    Applies (case-insensitive, substring):
      - sources.youtube.exclude_channels: filter by author_handle on YouTube candidates
      - sources.exclude_keywords:         filter by title across all platforms
    """
    all_candidates: list[dict] = []
    all_candidates.extend(discover_youtube(campaign_cfg, apify))
    all_candidates.extend(discover_tiktok(campaign_cfg, apify))
    all_candidates.extend(discover_instagram(campaign_cfg, apify))

    # ── Exclusion filters ─────────────────────────────────────────────────────
    exclude_keywords: list[str] = []
    try:
        exclude_keywords = list(campaign_cfg.sources.exclude_keywords or [])
    except AttributeError:
        pass

    yt_exclude_channels: list[str] = []
    try:
        yt_cfg = campaign_cfg.sources.youtube
        if yt_cfg is not None:
            yt_exclude_channels = list(yt_cfg.exclude_channels or [])
    except AttributeError:
        pass

    before_filter = len(all_candidates)
    filtered: list[dict] = []
    for c in all_candidates:
        # YouTube channel exclusion
        if c.get("platform") == "youtube" and yt_exclude_channels:
            handle = (c.get("author_handle") or "").lower()
            if any(exc.lower() in handle for exc in yt_exclude_channels):
                log.debug(
                    "YouTube candidate excluded by channel filter",
                    extra={"source_id": c.get("source_id"), "handle": handle},
                )
                continue
        # Cross-platform keyword exclusion
        if _is_excluded_by_keywords(c, exclude_keywords):
            continue
        filtered.append(c)

    if before_filter != len(filtered):
        log.info(
            "Discovery exclusion filters removed %d candidates",
            before_filter - len(filtered),
            extra={"campaign": campaign_cfg.name,
                   "before": before_filter, "after": len(filtered)},
        )

    # Final global dedup by source_id (unlikely but safe)
    seen: set[str] = set()
    unique = []
    for c in filtered:
        if c["source_id"] not in seen:
            seen.add(c["source_id"])
            unique.append(c)

    log.info(
        "Total discovery complete",
        extra={"campaign": campaign_cfg.name, "total_candidates": len(unique)},
    )
    return unique
