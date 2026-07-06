"""
scheduler/analytics.py — Entrypoint: python -m scheduler.analytics

Weekly analytics pull-back per SPEC §7.

For each enabled campaign where analytics.track is True:
  - Only runs when today's weekday matches analytics.pull_day (e.g. "monday"),
    unless --force is passed.
  - Pulls analytics from Postiz (GET /public/v1/posts/{id}/analytics) where
    exposed.
  - Scrapes destination profiles via core.apify:
      TikTok  — clockworks/free-tiktok-scraper
      Instagram — apify/instagram-scraper (resultsType=reels, onlyPostsNewerThan=1 week)
  - Matches scraped posts to clips rows via posted_permalinks first,
    then caption-prefix fallback.
  - Inserts/upserts analytics time-series rows.

Usage:
    python -m scheduler.analytics [--force] [--campaign <name>]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Guarded core imports
# ---------------------------------------------------------------------------
try:
    from core.db import get_session
except Exception:
    get_session = None  # type: ignore[assignment]

try:
    from core.models import Clip, Analytics
except Exception:
    Clip = None  # type: ignore[assignment]
    Analytics = None  # type: ignore[assignment]

try:
    from core.config import load_enabled_campaigns
except Exception:
    load_enabled_campaigns = None  # type: ignore[assignment]

try:
    from core.apify import Apify
except Exception:
    Apify = None  # type: ignore[assignment]

from scheduler.postiz import Postiz, PostizError, get_postiz_client

logger = logging.getLogger(__name__)

# Weekday name -> isoweekday (Monday=1 ... Sunday=7)
_WEEKDAY_MAP: dict[str, int] = {
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 7,
}


# ---------------------------------------------------------------------------
# Analytics pull entry
# ---------------------------------------------------------------------------

def run_analytics(
    *,
    campaign_filter: str | None = None,
    force: bool = False,
) -> None:
    if get_session is None or load_enabled_campaigns is None:
        logger.error(
            "Core modules not available — cannot run analytics. "
            "Ensure the package is installed correctly."
        )
        sys.exit(1)

    today_isoweekday = datetime.now(timezone.utc).isoweekday()  # 1=Mon, 7=Sun

    configs = load_enabled_campaigns()
    for cfg in configs:
        name: str = cfg.name if hasattr(cfg, "name") else cfg["name"]
        if campaign_filter and name != campaign_filter:
            continue

        analytics_cfg = cfg.analytics if hasattr(cfg, "analytics") else cfg.get("analytics", {})
        if not (analytics_cfg.track if hasattr(analytics_cfg, "track") else analytics_cfg.get("track", False)):
            logger.info("Analytics disabled for campaign %r; skipping", name)
            continue

        pull_day_str: str = (
            analytics_cfg.pull_day
            if hasattr(analytics_cfg, "pull_day")
            else analytics_cfg.get("pull_day", "monday")
        ).lower()
        pull_day_iso = _WEEKDAY_MAP.get(pull_day_str, 1)

        if not force and today_isoweekday != pull_day_iso:
            logger.info(
                "Campaign %r pull_day=%s (isoweekday %d), today is %d; skipping (use --force to override)",
                name,
                pull_day_str,
                pull_day_iso,
                today_isoweekday,
            )
            continue

        logger.info("Running analytics pull for campaign %r", name)
        _pull_campaign_analytics(cfg)


def _pull_campaign_analytics(cfg: Any) -> None:
    """Pull analytics for all posted clips in this campaign."""
    name: str = cfg.name if hasattr(cfg, "name") else cfg["name"]
    dest = cfg.destinations if hasattr(cfg, "destinations") else cfg["destinations"]
    channels: list[str] = (
        dest.postiz_channels
        if hasattr(dest, "postiz_channels")
        else dest["postiz_channels"]
    )

    # Load all posted clips for this campaign.
    if get_session is None or Clip is None:
        logger.error("DB not available; cannot pull analytics for %r", name)
        return

    with get_session() as session:
        posted_clips: list[Any] = (
            session.query(Clip)
            .filter(Clip.campaign == name, Clip.status == "posted")
            .all()
        )

    if not posted_clips:
        logger.info("No posted clips for campaign %r", name)
        return

    logger.info("Pulling analytics for %d posted clip(s) in campaign %r", len(posted_clips), name)

    # --- Postiz analytics ---
    try:
        with get_postiz_client() as postiz:
            _pull_postiz_analytics(posted_clips, postiz)
    except Exception as exc:
        logger.warning("Postiz analytics pull failed for %r: %s", name, exc)

    # --- Apify profile scraping ---
    _pull_apify_analytics(cfg, channels, posted_clips)


# ---------------------------------------------------------------------------
# Postiz analytics
# ---------------------------------------------------------------------------

def _pull_postiz_analytics(clips: list[Any], postiz: Postiz) -> None:
    """Pull Postiz analytics using the verified API.

    Strategy:
    1. Fetch GET /posts for the past 30 days to collect state + releaseURL.
    2. Match returned posts to clips by postiz_post_ids.
    3. If state=PUBLISHED, store releaseURL and flip clip.status to posted.
    4. Call GET /analytics/:integrationId?date=30 per integration for aggregate
       platform metrics (best-effort — not all Postiz versions expose per-post stats).
    """
    import json as _json

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=31)

    # Fetch recent posts from Postiz.
    try:
        postiz_posts = postiz.list_posts(start_date=start, end_date=now)
    except PostizError as exc:
        logger.warning("Could not fetch Postiz posts list: %s", exc)
        postiz_posts = []

    # Build lookup: postiz post id -> post record.
    postiz_by_id: dict[str, dict] = {}
    for pp in postiz_posts:
        pid = pp.get("id") or pp.get("postId") or ""
        if pid:
            postiz_by_id[str(pid)] = pp

    for clip in clips:
        if not clip.postiz_post_ids:
            continue
        try:
            post_ids: dict = (
                _json.loads(clip.postiz_post_ids)
                if isinstance(clip.postiz_post_ids, str)
                else clip.postiz_post_ids
            )
        except Exception:
            continue

        for channel, post_id in post_ids.items():
            if not post_id or post_id == "dry-run":
                continue
            pp = postiz_by_id.get(str(post_id))
            if pp is None:
                continue

            state: str = pp.get("state", "")
            release_url: str = pp.get("releaseURL") or ""

            # Flip clip to posted + store permalink.
            if state == "PUBLISHED":
                if release_url:
                    _update_permalink(clip, release_url)
                if clip.status != "posted":
                    _flip_clip_to_posted(clip)

            # Save analytics row from post data if metrics available.
            metrics = _extract_metrics(pp)
            if any(v > 0 for v in metrics.values()):
                _save_analytics_row(
                    clip_id=clip.id,
                    platform=_platform_from_channel(channel),
                    pulled_at=datetime.now(timezone.utc),
                    metrics=metrics,
                )

    # Per-integration analytics (aggregate, best-effort).
    _pull_integration_analytics(postiz)


# ---------------------------------------------------------------------------
# Apify profile scraping
# ---------------------------------------------------------------------------

def _pull_apify_analytics(cfg: Any, channels: list[str], clips: list[Any]) -> None:
    """Scrape destination profiles via Apify and match posts to clip rows."""
    if Apify is None:
        logger.warning("core.apify not available; skipping Apify analytics scrape")
        return

    from core.settings import get_settings

    if not get_settings().apify_token:
        logger.warning("APIFY_TOKEN not set; skipping Apify analytics scrape")
        return

    # Apify reads APIFY_TOKEN lazily via settings.require_apify()
    apify = Apify()

    for channel in channels:
        platform = _platform_from_channel(channel)
        if platform == "tiktok":
            _scrape_tiktok(cfg, channel, clips, apify)
        elif platform == "instagram":
            _scrape_instagram(cfg, channel, clips, apify)
        else:
            logger.debug("No Apify analytics scraper for platform=%s channel=%s", platform, channel)


def _pull_integration_analytics(postiz: Postiz) -> None:
    """Call GET /analytics/:integrationId?date=30 for each integration."""
    try:
        integrations = postiz.list_integrations()
    except PostizError as exc:
        logger.debug("Could not list integrations for analytics: %s", exc)
        return
    for integration in integrations:
        iid = integration.get("id", "")
        if not iid:
            continue
        try:
            data = postiz.get_integration_analytics(iid, days=30)
            if data:
                logger.debug(
                    "Integration analytics id=%s identifier=%s: %s",
                    iid,
                    integration.get("identifier", ""),
                    data,
                )
        except Exception as exc:
            logger.debug("Integration analytics failed id=%s: %s", iid, exc)


def _flip_clip_to_posted(clip: Any) -> None:
    """Set clip.status = 'posted' in the database."""
    if get_session is None or Clip is None:
        return
    try:
        with get_session() as session:
            db_clip = session.get(Clip, clip.id)
            if db_clip is not None:
                db_clip.status = "posted"
                session.commit()
                logger.info("Clip %s flipped to status=posted", clip.id)
    except Exception as exc:
        logger.error("Failed to flip clip %s to posted: %s", clip.id, exc)


def _scrape_tiktok(cfg: Any, channel: str, clips: list[Any], apify: Any) -> None:
    """Scrape TikTok profile reels and match to clips."""
    profile_url = _resolve_destination_profile_url(cfg, "tiktok")
    if not profile_url:
        logger.info("No TikTok profile URL configured for analytics on channel %s", channel)
        return

    logger.info("Scraping TikTok profile %s for analytics", profile_url)
    try:
        items = apify.run(
            "clockworks/free-tiktok-scraper",
            {
                "profiles": [profile_url],
                "resultsType": "posts",
                "maxPostCount": 50,
            },
        )
    except Exception as exc:
        logger.error("TikTok Apify scrape failed: %s", exc)
        return

    for item in items:
        _match_and_save(item, clips, platform="tiktok", url_field="webVideoUrl")


def _scrape_instagram(cfg: Any, channel: str, clips: list[Any], apify: Any) -> None:
    """Scrape Instagram recent reels and match to clips."""
    profile_url = _resolve_destination_profile_url(cfg, "instagram")
    if not profile_url:
        logger.info("No Instagram profile URL configured for analytics on channel %s", channel)
        return

    logger.info("Scraping Instagram profile %s for analytics", profile_url)
    try:
        items = apify.run(
            "apify/instagram-scraper",
            {
                "directUrls": [profile_url],
                "resultsType": "reels",
                "onlyPostsNewerThan": "1 week",
            },
        )
    except Exception as exc:
        logger.error("Instagram Apify scrape failed: %s", exc)
        return

    for item in items:
        _match_and_save(item, clips, platform="instagram", url_field="url")


# ---------------------------------------------------------------------------
# Matching + persistence
# ---------------------------------------------------------------------------

def _match_and_save(
    scraped: dict[str, Any],
    clips: list[Any],
    *,
    platform: str,
    url_field: str,
) -> None:
    """Match a scraped post to a clip row and save analytics."""
    scraped_url: str = scraped.get(url_field) or scraped.get("url") or ""
    clip = _match_by_permalink(scraped_url, clips) or _match_by_caption_prefix(scraped, clips)

    if clip is None:
        logger.debug("Could not match scraped post url=%s to any clip", scraped_url)
        return

    # Update posted_permalinks if we have a URL match.
    if scraped_url:
        _update_permalink(clip, scraped_url)

    metrics = _extract_metrics(scraped)
    _save_analytics_row(
        clip_id=clip.id,
        platform=platform,
        pulled_at=datetime.now(timezone.utc),
        metrics=metrics,
    )


def _match_by_permalink(url: str, clips: list[Any]) -> Any | None:
    if not url:
        return None
    for clip in clips:
        try:
            permalinks = clip.posted_permalinks or {}
            if isinstance(permalinks, str):
                import json as _json
                permalinks = _json.loads(permalinks)
            for _, purl in permalinks.items():
                if purl and (purl == url or url in purl or purl in url):
                    return clip
        except Exception:
            continue
    return None


def _match_by_caption_prefix(scraped: dict, clips: list[Any]) -> Any | None:
    """Fallback: match by leading 60 chars of caption."""
    scraped_caption: str = (
        scraped.get("text") or scraped.get("caption") or scraped.get("desc") or ""
    ).strip()
    if not scraped_caption:
        return None
    prefix_len = 60
    scraped_prefix = scraped_caption[:prefix_len].lower()

    for clip in clips:
        if not clip.caption:
            continue
        clip_prefix = clip.caption[:prefix_len].lower()
        if clip_prefix and clip_prefix == scraped_prefix:
            return clip
    return None


def _extract_metrics(scraped: dict) -> dict[str, int]:
    return {
        "views": int(scraped.get("playCount") or scraped.get("videoViewCount") or scraped.get("views") or 0),
        "likes": int(scraped.get("diggCount") or scraped.get("likesCount") or scraped.get("likes") or 0),
        "comments": int(scraped.get("commentCount") or scraped.get("commentsCount") or scraped.get("comments") or 0),
        "shares": int(scraped.get("shareCount") or scraped.get("shares") or 0),
    }


def _save_analytics_row(
    *,
    clip_id: str,
    platform: str,
    pulled_at: datetime,
    metrics: dict,
) -> None:
    if get_session is None or Analytics is None:
        return
    try:
        pulled_at_utc = pulled_at.astimezone(timezone.utc)
        with get_session() as session:
            row = Analytics(
                clip_id=clip_id,
                platform=platform,
                pulled_at=pulled_at_utc,
                views=metrics.get("views", 0),
                likes=metrics.get("likes", 0),
                comments=metrics.get("comments", 0),
                shares=metrics.get("shares", 0),
            )
            session.add(row)
            session.commit()
            logger.debug(
                "Saved analytics: clip=%s platform=%s views=%s likes=%s",
                clip_id,
                platform,
                metrics.get("views"),
                metrics.get("likes"),
            )
    except Exception as exc:
        logger.error("Failed to save analytics row clip=%s: %s", clip_id, exc)


def _update_permalink(clip: Any, url: str) -> None:
    """Add url to clip.posted_permalinks if not already present."""
    if get_session is None:
        return
    try:
        import json as _json
        with get_session() as session:
            db_clip = session.get(Clip, clip.id)
            if db_clip is None:
                return
            permalinks = db_clip.posted_permalinks or {}
            if isinstance(permalinks, str):
                permalinks = _json.loads(permalinks)
            # Key by platform derived from URL (best-effort).
            key = "scraped"
            if "tiktok" in url:
                key = "tiktok"
            elif "instagram" in url or "instagr.am" in url:
                key = "instagram"
            elif "twitter.com" in url or "x.com" in url:
                key = "x"
            if key not in permalinks:
                permalinks[key] = url
                db_clip.posted_permalinks = permalinks
                session.commit()
    except Exception as exc:
        logger.warning("Could not update permalink for clip %s: %s", clip.id, exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _platform_from_channel(channel: str) -> str:
    """Infer platform string from a channel name (best-effort)."""
    lower = channel.lower()
    if "tiktok" in lower:
        return "tiktok"
    if "instagram" in lower or "ig_" in lower:
        return "instagram"
    if "twitter" in lower or "_x_" in lower or lower.endswith("_x"):
        return "x"
    return lower.split("_")[0]


def _resolve_destination_profile_url(cfg: Any, platform: str) -> str | None:
    """Extract a profile URL for a platform from the campaign config."""
    try:
        sources = cfg.sources if hasattr(cfg, "sources") else cfg.get("sources", {})
        platform_sources = (
            sources.__dict__ if hasattr(sources, "__dict__") else sources
        )
        ps = platform_sources.get(platform, {})
        profiles = ps.profiles if hasattr(ps, "profiles") else ps.get("profiles", [])
        if profiles:
            return profiles[0]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Pull analytics for posted clips")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run regardless of today's weekday vs pull_day",
    )
    parser.add_argument(
        "--campaign",
        help="Limit to a single campaign by name",
        default=None,
    )
    args = parser.parse_args()

    run_analytics(campaign_filter=args.campaign, force=args.force)


if __name__ == "__main__":
    main()
