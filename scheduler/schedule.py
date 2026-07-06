"""
scheduler/schedule.py — Entrypoint: python -m scheduler.schedule

Picks up clips with status=approved, computes the next open slot per campaign
schedule, uploads each clip to Postiz, creates a post (or draft), records
postiz_post_ids + scheduled_at, then flips status to scheduled.

Idempotent: a clip that already has postiz_post_ids set is skipped — it can
never be double-posted.

Usage:
    python -m scheduler.schedule [--campaign <name>] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Guarded core imports — modules may not exist yet during parallel builds.
# ---------------------------------------------------------------------------
try:
    from core.db import get_session
except Exception:
    get_session = None  # type: ignore[assignment]

try:
    from core.models import Clip, Campaign
except Exception:
    Clip = None  # type: ignore[assignment]
    Campaign = None  # type: ignore[assignment]

try:
    from core.config import load_enabled_campaigns, CampaignConfig
except Exception:
    load_enabled_campaigns = None  # type: ignore[assignment]
    CampaignConfig = None  # type: ignore[assignment]

try:
    import pytz
    _HAS_PYTZ = True
except ImportError:
    _HAS_PYTZ = False

from scheduler.postiz import Postiz, PostizError, get_postiz_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slot computation
# ---------------------------------------------------------------------------

def _parse_time(t: str) -> time:
    """Parse 'HH:MM' or 'HH:MM:SS' to a datetime.time object."""
    parts = t.strip().split(":")
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) > 2 else 0
    return time(hour, minute, second)


def _localized_now(tz_name: str) -> datetime:
    """Return current datetime localised to the given IANA timezone."""
    if _HAS_PYTZ:
        import pytz as _pytz
        tz = _pytz.timezone(tz_name)
        return datetime.now(tz)
    # Fallback: use UTC offset via zoneinfo (Python 3.9+).
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
        return datetime.now(tz)
    except Exception:
        logger.warning("Cannot resolve timezone %r; using UTC", tz_name)
        return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    """Convert a timezone-aware datetime to UTC."""
    return dt.astimezone(timezone.utc)


def compute_next_slot(
    campaign_cfg: Any,
    taken_utc_datetimes: list[datetime],
) -> datetime:
    """Return the next open posting slot for *campaign_cfg*.

    Iterates over schedule.times × posts_per_day starting from now.
    A slot is "taken" if any datetime in *taken_utc_datetimes* falls within
    60 seconds of it.

    Args:
        campaign_cfg:         CampaignConfig (or duck-typed dict with .destinations).
        taken_utc_datetimes:  UTC datetimes of already-scheduled/posted clips
                              on this campaign+channel combination.
    Returns:
        UTC datetime of the next open slot (>= now).
    """
    dest = campaign_cfg.destinations if hasattr(campaign_cfg, "destinations") else campaign_cfg["destinations"]
    sched = dest.schedule if hasattr(dest, "schedule") else dest["schedule"]

    tz_name: str = sched.timezone if hasattr(sched, "timezone") else sched["timezone"]
    times_raw: list[str] = sched.times if hasattr(sched, "times") else sched["times"]
    posts_per_day: int = sched.posts_per_day if hasattr(sched, "posts_per_day") else sched["posts_per_day"]

    slot_times = sorted([_parse_time(t) for t in times_raw])[:posts_per_day]

    now_local = _localized_now(tz_name)
    taken_ts = {dt.replace(second=0, microsecond=0) for dt in taken_utc_datetimes}

    # Search up to 90 days ahead.
    for day_offset in range(90):
        check_date = (now_local + timedelta(days=day_offset)).date()
        for slot_time in slot_times:
            try:
                from zoneinfo import ZoneInfo
                tz_obj = ZoneInfo(tz_name)
            except Exception:
                tz_obj = timezone.utc  # type: ignore[assignment]

            slot_local = datetime.combine(check_date, slot_time, tzinfo=tz_obj)
            slot_utc = _to_utc(slot_local)

            # Must be in the future.
            if slot_utc <= datetime.now(timezone.utc):
                continue

            # Must not be taken.
            slot_rounded = slot_utc.replace(second=0, microsecond=0)
            if slot_rounded not in taken_ts:
                return slot_utc

    raise RuntimeError("Could not find an open posting slot within 90 days")


# ---------------------------------------------------------------------------
# Caption rendering
# ---------------------------------------------------------------------------

def _render_caption(campaign_cfg: Any, clip: Any) -> str:
    """Interpolate the campaign caption_template with clip metadata."""
    try:
        dest = campaign_cfg.destinations if hasattr(campaign_cfg, "destinations") else campaign_cfg["destinations"]
        tmpl: str = dest.caption_template if hasattr(dest, "caption_template") else dest["caption_template"]
        hashtags_list: list[str] = dest.hashtags if hasattr(dest, "hashtags") else dest["hashtags"]
        hashtags_str = " ".join(hashtags_list)

        hook = clip.hook or ""
        source_handle = ""
        # Attempt to resolve source_handle from the related Source row.
        try:
            src = getattr(clip, "source_rel", None) or getattr(clip, "source", None)
            if src:
                # Column may be named source_metadata (reserved word rename) or metadata.
                meta = (
                    getattr(src, "source_metadata", None)
                    or getattr(src, "meta", None)
                    or getattr(src, "metadata", None)
                    or {}
                ) or {}
                source_handle = (
                    meta.get("channelName")
                    or meta.get("authorMeta", {}).get("name")
                    or getattr(src, "author_handle", None)
                    or ""
                )
        except Exception:
            pass

        return tmpl.format(
            hook=hook,
            source_handle=source_handle,
            hashtags=hashtags_str,
        )
    except Exception as exc:
        logger.warning("Caption render failed: %s; using hook as caption", exc)
        return clip.hook or ""


# ---------------------------------------------------------------------------
# Main scheduling logic
# ---------------------------------------------------------------------------

def schedule_approved_clips(
    *,
    campaign_filter: str | None = None,
    dry_run: bool = False,
) -> None:
    """Main scheduling pass.

    For each approved clip:
    1. Resolve its campaign config.
    2. Compute the next open slot per destination channel.
    3. Upload to Postiz + create post/draft.
    4. Persist postiz_post_ids + scheduled_at, set status=scheduled.
    """
    if get_session is None or Clip is None or load_enabled_campaigns is None:
        logger.error(
            "Core modules not available — cannot run scheduler. "
            "Ensure the package is installed correctly."
        )
        sys.exit(1)

    configs = {cfg.name: cfg for cfg in load_enabled_campaigns()}
    if campaign_filter and campaign_filter not in configs:
        logger.error("Campaign %r not found or not enabled", campaign_filter)
        sys.exit(1)

    with get_session() as session:
        query = session.query(Clip).filter(Clip.status == "approved")
        if campaign_filter:
            query = query.filter(Clip.campaign == campaign_filter)
        approved_clips: list[Any] = query.all()

    logger.info("Found %d approved clip(s) to schedule", len(approved_clips))
    if not approved_clips:
        return

    with get_postiz_client() as postiz:
        for clip in approved_clips:
            _process_clip(clip, configs, postiz, dry_run=dry_run)


def _process_clip(
    clip: Any,
    configs: dict[str, Any],
    postiz: Postiz,
    *,
    dry_run: bool,
) -> None:
    """Schedule a single clip across all its destination channels."""
    # Idempotency guard: never re-post a clip that already has Postiz ids.
    existing_ids: dict = {}
    if clip.postiz_post_ids:
        try:
            existing_ids = json.loads(clip.postiz_post_ids) if isinstance(clip.postiz_post_ids, str) else clip.postiz_post_ids
        except Exception:
            pass
    if existing_ids:
        logger.info(
            "Clip %s already has postiz_post_ids — skipping (idempotency guard)",
            clip.id,
        )
        return

    cfg = configs.get(clip.campaign)
    if cfg is None:
        logger.warning(
            "No enabled config found for campaign %r; skipping clip %s",
            clip.campaign,
            clip.id,
        )
        return

    video_path = Path(clip.file_path) if clip.file_path else None
    if not video_path or not video_path.exists():
        logger.warning(
            "Video file missing for clip %s: %s — skipping",
            clip.id,
            clip.file_path,
        )
        return

    dest = cfg.destinations if hasattr(cfg, "destinations") else cfg["destinations"]
    channels: list[str] = (
        dest.postiz_channels
        if hasattr(dest, "postiz_channels")
        else dest["postiz_channels"]
    )
    autopost: bool = dest.autopost if hasattr(dest, "autopost") else dest.get("autopost", False)
    draft = not autopost

    caption = _render_caption(cfg, clip)

    # Gather already-taken slots for this campaign across all channels.
    taken_utc: list[datetime] = _load_taken_slots(clip.campaign)

    new_post_ids: dict[str, str] = {}
    scheduled_at: datetime | None = None

    for channel in channels:
        try:
            slot = compute_next_slot(cfg, taken_utc)
        except RuntimeError as exc:
            logger.error("Could not compute slot for clip %s channel %s: %s", clip.id, channel, exc)
            continue

        logger.info(
            "Scheduling clip %s -> channel=%s slot=%s draft=%s dry_run=%s",
            clip.id,
            channel,
            slot.isoformat(),
            draft,
            dry_run,
        )

        if not dry_run:
            try:
                result = postiz.create_post(
                    channel=channel,
                    caption=caption,
                    video_path=video_path,
                    schedule_at=slot,
                    draft=draft,
                )
                post_id = str(result.get("id", ""))
                new_post_ids[channel] = post_id
                taken_utc.append(slot)
                if scheduled_at is None:
                    scheduled_at = slot
                logger.info(
                    "Postiz post created id=%s clip=%s channel=%s",
                    post_id,
                    clip.id,
                    channel,
                )
            except PostizError as exc:
                logger.error(
                    "Postiz error for clip %s channel %s: %s",
                    clip.id,
                    channel,
                    exc,
                )
        else:
            # Dry run: mark as if taken so subsequent channels get different slots.
            taken_utc.append(slot)
            new_post_ids[channel] = "dry-run"
            if scheduled_at is None:
                scheduled_at = slot

    if new_post_ids and not dry_run:
        _persist_scheduled(clip, new_post_ids, scheduled_at)



def _load_taken_slots(campaign: str) -> list[datetime]:
    """Return UTC scheduled_at datetimes for all scheduled/posted clips in *campaign*."""
    if get_session is None or Clip is None:
        return []
    try:
        with get_session() as session:
            # scheduled_at may not be on the Clip model in all versions —
            # check with hasattr before querying the column.
            if not hasattr(Clip, "scheduled_at"):
                return []
            rows = (
                session.query(Clip.scheduled_at)
                .filter(
                    Clip.campaign == campaign,
                    Clip.status.in_(["scheduled", "posted"]),
                    Clip.scheduled_at.isnot(None),
                )
                .all()
            )
        result = []
        for (dt,) in rows:
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                result.append(dt)
        return result
    except Exception as exc:
        logger.warning("Could not load taken slots: %s", exc)
        return []


def _persist_scheduled(
    clip: Any,
    post_ids: dict[str, str],
    scheduled_at: datetime | None,
) -> None:
    """Persist Postiz post ids and flip clip status to scheduled."""
    if get_session is None:
        return
    try:
        with get_session() as session:
            db_clip = session.get(Clip, clip.id)
            if db_clip is None:
                logger.error("Clip %s disappeared from DB during scheduling", clip.id)
                return
            db_clip.postiz_post_ids = post_ids
            db_clip.status = "scheduled"
            if scheduled_at is not None:
                # scheduled_at column may not exist on all model versions; set defensively.
                try:
                    db_clip.scheduled_at = scheduled_at.astimezone(timezone.utc)
                except AttributeError:
                    pass
            session.commit()
            logger.info("Clip %s marked as scheduled", clip.id)
    except Exception as exc:
        logger.error("DB persist failed for clip %s: %s", clip.id, exc)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Schedule approved clips via Postiz"
    )
    parser.add_argument(
        "--campaign",
        help="Only schedule clips for this campaign name",
        default=None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without uploading or creating Postiz posts",
    )
    args = parser.parse_args()

    schedule_approved_clips(
        campaign_filter=args.campaign,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
