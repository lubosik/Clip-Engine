"""
meme/feedback.py — weekly feedback loop for meme profile improvement.

Queries posted meme Clips ranked by engagement (views + likes), downloads
their images, copies them into the campaign's refs_dir, then calls
extract_profile to produce a new profile version.

This is the only "training" mechanism — no fine-tuning.

Public API:
    promote_top_performers(campaign_cfg, session, top_n=3) -> MemeProfile | None

CLI:
    python -m meme.feedback <campaign_name> [--top-n N]
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from core.models import Analytics, Clip, MemeProfile
from meme.profile import extract_profile, get_active_profile

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Image download helper
# ---------------------------------------------------------------------------

def _fetch_image(file_path: str, campaign: str) -> bytes | None:
    """
    Return raw image bytes for a Clip.file_path value.

    Handles both local paths and r2:// references.
    Returns None on error.
    """
    from core.storage import media_ref_is_r2

    if not file_path:
        return None

    if media_ref_is_r2(file_path):
        # Strip the r2:// prefix to get the object key
        key = file_path[len("r2://"):]
        try:
            from core import r2
            import tempfile, os

            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp_path = tmp.name

            r2.download_file(key, tmp_path)
            data = Path(tmp_path).read_bytes()
            os.unlink(tmp_path)
            return data
        except Exception as exc:
            log.warning(
                "Failed to download meme image from R2: key=%s error=%s",
                key,
                exc,
            )
            return None
    else:
        local = Path(file_path)
        if local.exists():
            try:
                return local.read_bytes()
            except OSError as exc:
                log.warning(
                    "Failed to read local meme image: path=%s error=%s",
                    file_path,
                    exc,
                )
        return None


# ---------------------------------------------------------------------------
# Top performer query
# ---------------------------------------------------------------------------

def _get_top_performer_clips(
    campaign: str,
    session: Any,
    top_n: int,
) -> list[Any]:
    """
    Query the database for the top posted meme clips by engagement.

    Engagement = views + likes (latest Analytics row per clip).
    Returns a list of Clip ORM objects.
    """
    from sqlalchemy import func

    # Get the most recent analytics row per clip
    latest_subq = (
        session.query(
            Analytics.clip_id,
            func.max(Analytics.pulled_at).label("latest_at"),
        )
        .group_by(Analytics.clip_id)
        .subquery()
    )

    top_clips = (
        session.query(Clip)
        .join(
            Analytics,
            (Analytics.clip_id == Clip.id)
            & (Analytics.pulled_at == latest_subq.c.latest_at),
        )
        .join(
            latest_subq,
            latest_subq.c.clip_id == Clip.id,
        )
        .filter(
            Clip.campaign == campaign,
            Clip.kind == "meme",
            Clip.status == "posted",
        )
        .order_by((Analytics.views + Analytics.likes).desc())
        .limit(top_n)
        .all()
    )

    return top_clips


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def promote_top_performers(
    campaign_cfg: Any,
    session: Any,
    *,
    top_n: int = 3,
) -> MemeProfile | None:
    """
    Promote top-performing memes into the reference set and re-extract profile.

    Steps:
      1. Query Analytics + Clips: find top *top_n* posted meme clips by
         (views + likes) for this campaign.
      2. Download / copy their images to refs_dir.
      3. Call extract_profile to create a new profile version.

    Args:
        campaign_cfg: CampaignConfig instance.
        session:      SQLAlchemy session.
        top_n:        Number of top performers to add to refs_dir.

    Returns:
        The new MemeProfile row, or None if no posted memes were found.
    """
    campaign_name = campaign_cfg.name

    if not campaign_cfg.meme or not campaign_cfg.meme.refs_dir:
        log.warning(
            "No meme.refs_dir configured for campaign '%s'; "
            "cannot promote top performers",
            campaign_name,
        )
        return None

    refs_dir = Path(campaign_cfg.meme.refs_dir)
    refs_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "Querying top posted meme performers",
        extra={"campaign": campaign_name, "top_n": top_n},
    )

    top_clips = _get_top_performer_clips(campaign_name, session, top_n)

    if not top_clips:
        log.info(
            "No posted memes with analytics found for campaign '%s'; "
            "skipping feedback loop",
            campaign_name,
        )
        return None

    log.info(
        "Found %d top performer(s); downloading images",
        len(top_clips),
        extra={"campaign": campaign_name},
    )

    promoted = 0
    for clip in top_clips:
        if not clip.file_path:
            log.warning(
                "Top performer clip %d has no file_path; skipping",
                clip.id,
            )
            continue

        image_bytes = _fetch_image(clip.file_path, campaign_name)
        if image_bytes is None:
            log.warning(
                "Could not retrieve image for clip %d; skipping", clip.id
            )
            continue

        dest_name = f"promoted_{clip.id}.png"
        dest_path = refs_dir / dest_name
        try:
            dest_path.write_bytes(image_bytes)
            log.info(
                "Promoted meme image to refs_dir: %s", dest_path
            )
            promoted += 1
        except OSError as exc:
            log.warning(
                "Failed to write promoted image to refs_dir: %s error=%s",
                dest_path,
                exc,
            )

    if promoted == 0:
        log.warning(
            "No images could be promoted for campaign '%s'", campaign_name
        )
        return None

    log.info(
        "Extracting new profile version after promoting %d image(s)",
        promoted,
        extra={"campaign": campaign_name},
    )

    try:
        new_profile = extract_profile(campaign_cfg, session)
        session.commit()
        log.info(
            "New profile version %d created for campaign '%s'",
            new_profile.version,
            campaign_name,
        )
        return new_profile
    except Exception as exc:
        log.error(
            "Profile re-extraction failed after promotion: %s", exc,
            extra={"campaign": campaign_name},
            exc_info=True,
        )
        raise


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    from core.logging import configure_logging
    from core.config import load_campaign
    from core.db import get_session

    configure_logging()

    parser = argparse.ArgumentParser(
        description="Meme feedback loop — promote top performers and re-extract profile"
    )
    parser.add_argument("campaign", help="Campaign name (filename without .yaml)")
    parser.add_argument(
        "--top-n",
        type=int,
        default=3,
        help="Number of top performers to promote (default: 3)",
    )
    args = parser.parse_args()

    campaign_path = Path("campaigns") / f"{args.campaign}.yaml"
    if not campaign_path.exists():
        log.error("Campaign YAML not found: %s", campaign_path)
        sys.exit(1)

    campaign_cfg = load_campaign(campaign_path, strict_assets=False)

    if not campaign_cfg.engines.memes:
        log.warning(
            "engines.memes is false for campaign '%s'; "
            "set engines.memes: true to run the meme engine",
            args.campaign,
        )

    with get_session() as session:
        result = promote_top_performers(
            campaign_cfg,
            session,
            top_n=args.top_n,
        )
        if result:
            log.info(
                "Feedback loop complete; new profile version=%d",
                result.version,
            )
        else:
            log.info("Feedback loop: nothing to promote")


if __name__ == "__main__":
    main()
