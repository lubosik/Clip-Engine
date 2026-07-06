"""
producer/run.py — campaign pipeline entrypoint.

Usage:
    python -m producer.run <campaign_name>
    python -m producer.run --all

Orchestrates SPEC §4 stages 1–5 + download + render + queue:
    1. Discover sources
    2. Dedupe (source level)
    3. Comment signal (TikTok only, optional)
    4. Transcribe selected sources
    5. Rank → select clips (non-overlap, exhaust_source loop)
    6. Download source video
    7. Render clip (calls producer.render.render_clip — owned by BACKEND-RENDER)
    8. Insert clips row status=pending_review
    9. Mark source done/partially_done, update used_ranges, clean up raw files

Each source is wrapped in its own try/except so one failure never kills the run.
Structured logging throughout; ffmpeg parallelism capped at cpu_count.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Logging must be configured before any other import that uses it
from core.logging import configure_logging

configure_logging()
log = logging.getLogger(__name__)


def _render_clip_import():
    """Lazy import of render_clip — owned by BACKEND-RENDER agent."""
    from producer.render import render_clip  # noqa: F401
    return render_clip


def _build_caption(template: str, hook: str, source_handle: str, hashtags: list[str]) -> str:
    hashtag_str = " ".join(h if h.startswith("#") else f"#{h}" for h in hashtags)
    return template.format(
        hook=hook,
        source_handle=source_handle,
        hashtags=hashtag_str,
    ).strip()


def _process_source(
    source: dict,
    campaign_cfg: Any,
    apify: Any,
    session: Any,
    cpu_count: int,
) -> list[dict]:
    """
    Process a single source through stages 3–8.
    Returns list of inserted clip dicts (may be empty).
    Returns [] on any error (logs the error).
    """
    from core.models import Source as SourceModel, Clip
    from producer.dedupe import mark_source_status, update_used_ranges
    from producer.comments import pull_and_store_comments
    from producer.transcripts import fetch_and_store_transcript
    from producer.ranker import rank_clips, select_clips
    from producer.download import download_source, cleanup_source
    from core.storage import work_dir

    source_id: str = source["source_id"]
    platform: str = source["platform"]
    url: str = source["url"]
    campaign_name: str = campaign_cfg.name

    try:
        log.info("Processing source", extra={"source_id": source_id, "platform": platform})

        # Load existing used_ranges from DB
        source_row = session.query(SourceModel).filter_by(source_id=source_id).first()
        used_ranges: list[list[float]] = source_row.used_ranges if source_row and source_row.used_ranges else []

        # Stage 3: comment signal (TikTok only)
        comment_summary: str | None = None
        if platform == "tiktok":
            try:
                comment_summary = pull_and_store_comments(
                    session=session,
                    source_id=source_id,
                    post_url=url,
                    apify=apify,
                )
            except Exception as exc:
                log.warning(
                    "Comment pull failed; continuing without comment signal",
                    extra={"source_id": source_id, "error": str(exc)},
                )

        # Stage 4: transcript
        ig_raw = source.get("raw") if platform == "instagram" else None
        segments = fetch_and_store_transcript(
            session=session,
            source_id=source_id,
            platform=platform,
            url=url,
            apify=apify,
            ig_raw_item=ig_raw,
        )
        session.commit()

        if not segments:
            log.warning(
                "No transcript; skipping source",
                extra={"source_id": source_id},
            )
            mark_source_status(session, source_id, "done")
            session.commit()
            return []

        # Stage 5: rank → select
        candidates = rank_clips(segments, comment_summary, campaign_cfg.ranking)
        selected = select_clips(candidates, used_ranges, campaign_cfg.ranking)

        if not selected:
            log.info(
                "No clips selected from source",
                extra={"source_id": source_id, "candidates": len(candidates)},
            )
            mark_source_status(session, source_id, "done")
            session.commit()
            return []

        # Mark selected so we don't pick it up again mid-run
        mark_source_status(session, source_id, "selected")
        session.commit()

        # Stage 6: download
        source_video_path = download_source(
            source_id=source_id,
            platform=platform,
            url=url,
            raw=source.get("raw", {}),
        )

        # Stage 7 & 8: render + insert clip rows
        render_clip = _render_clip_import()
        inserted_clips: list[dict] = []
        new_ranges: list[list[float]] = []

        wdir = work_dir(source_id)

        for clip_candidate in selected:
            try:
                result = render_clip(
                    cfg=campaign_cfg,
                    source_meta=source,
                    clip=clip_candidate,
                    source_video=Path(source_video_path),
                    words=None,  # render agent runs faster-whisper on the cut clip
                    workdir=wdir,
                )

                caption = _build_caption(
                    template=campaign_cfg.destinations.caption_template,
                    hook=clip_candidate.get("hook", ""),
                    source_handle=source.get("author_handle") or "",
                    hashtags=campaign_cfg.destinations.hashtags,
                )

                clip_row = Clip(
                    campaign=campaign_name,
                    source_id=source_id,
                    start=clip_candidate["start"],
                    end=clip_candidate["end"],
                    hook=clip_candidate.get("hook"),
                    score=clip_candidate.get("score"),
                    reason=clip_candidate.get("reason"),
                    file_path=str(result.final_path),
                    thumb_path=str(result.thumb_path),
                    caption=caption,
                    destination_channels=campaign_cfg.destinations.postiz_channels,
                    status="pending_review",
                )
                session.add(clip_row)
                session.flush()  # get the id

                new_ranges.append([clip_candidate["start"], clip_candidate["end"]])
                inserted_clips.append({"clip_id": clip_row.id, "source_id": source_id})

                log.info(
                    "Clip rendered and queued",
                    extra={
                        "clip_id": clip_row.id,
                        "source_id": source_id,
                        "start": clip_candidate["start"],
                        "end": clip_candidate["end"],
                        "score": clip_candidate.get("score"),
                    },
                )
            except Exception as exc:
                log.error(
                    "Clip render failed; skipping this clip",
                    extra={
                        "source_id": source_id,
                        "start": clip_candidate.get("start"),
                        "end": clip_candidate.get("end"),
                        "error": str(exc),
                    },
                )

        # Update used_ranges
        if new_ranges:
            update_used_ranges(session, source_id, new_ranges)

        # Determine final source status
        if campaign_cfg.ranking.exhaust_source:
            # Caller (exhaust loop) will set done; mark partially_done for now
            new_status = "partially_done"
        else:
            new_status = "done"
        mark_source_status(session, source_id, new_status)
        session.commit()

        # Stage 9: cleanup raw file
        try:
            cleanup_source(source_id)
        except Exception as exc:
            log.warning(
                "Raw file cleanup failed (non-fatal)",
                extra={"source_id": source_id, "error": str(exc)},
            )

        return inserted_clips

    except Exception as exc:
        log.error(
            "Source processing failed",
            extra={"source_id": source_id, "platform": platform, "error": str(exc)},
            exc_info=True,
        )
        try:
            session.rollback()
        except Exception:
            pass
        return []


def run_campaign(campaign_name: str) -> None:
    """Execute a full pipeline run for one campaign."""
    from core.config import load_campaign
    from core.apify import Apify
    from core.db import get_session
    from producer.dedupe import (
        get_existing_source_ids,
        get_done_source_ids,
        filter_new_candidates,
        filter_done_sources,
        sort_by_engagement,
        upsert_source,
    )
    from producer.discover import discover_all

    campaign_path = Path("campaigns") / f"{campaign_name}.yaml"
    if not campaign_path.exists():
        log.error(
            "Campaign YAML not found",
            extra={"campaign": campaign_name, "path": str(campaign_path)},
        )
        sys.exit(1)

    log.info("Loading campaign", extra={"campaign": campaign_name})
    try:
        campaign_cfg = load_campaign(campaign_path, strict_assets=True)
    except (FileNotFoundError, ValueError) as exc:
        log.error(
            "Campaign config failed",
            extra={"campaign": campaign_name, "error": str(exc)},
        )
        sys.exit(1)

    if not campaign_cfg.enabled:
        log.info("Campaign is disabled; skipping", extra={"campaign": campaign_name})
        return

    apify = Apify()

    run_start = datetime.now(tz=timezone.utc)
    log.info("Campaign run starting", extra={"campaign": campaign_name, "run_start": run_start.isoformat()})

    # Stage 1: discover
    candidates = discover_all(campaign_cfg, apify)
    if not candidates:
        log.warning("No candidates discovered", extra={"campaign": campaign_name})
        return

    with get_session() as session:
        # Stage 2: dedupe
        existing_ids = get_existing_source_ids(session, campaign_name)
        done_ids = get_done_source_ids(session, campaign_name)

        new_candidates = filter_new_candidates(candidates, existing_ids)
        # Also include partially_done sources (exhaust_source logic)
        not_done = filter_done_sources(candidates, done_ids)

        # Merge: new + partially_done that weren't in new
        new_ids = {c["source_id"] for c in new_candidates}
        to_process = new_candidates + [c for c in not_done if c["source_id"] not in new_ids]
        to_process = sort_by_engagement(to_process)

        if not to_process:
            log.info("All candidates already processed", extra={"campaign": campaign_name})
            return

        # Upsert source rows for new candidates
        for candidate in new_candidates:
            upsert_source(session, candidate, campaign_name)
        session.commit()

        log.info(
            "Sources ready for processing",
            extra={"campaign": campaign_name, "count": len(to_process)},
        )

    # Process each source with limited parallelism
    # ffmpeg jobs are CPU-bound; cap at cpu_count
    cpu_count = os.cpu_count() or 2

    total_clips = 0
    for source in to_process:
        # Each source gets its own session to isolate failures
        with get_session() as session:
            clips = _process_source(source, campaign_cfg, apify, session, cpu_count)
            total_clips += len(clips)

    run_end = datetime.now(tz=timezone.utc)
    elapsed = (run_end - run_start).total_seconds()
    log.info(
        "Campaign run complete",
        extra={
            "campaign": campaign_name,
            "sources_processed": len(to_process),
            "clips_queued": total_clips,
            "elapsed_sec": round(elapsed, 1),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clip Engine producer — run a campaign pipeline"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("campaign", nargs="?", help="Campaign name (filename without .yaml)")
    group.add_argument("--all", action="store_true", help="Run all enabled campaigns")
    args = parser.parse_args()

    if args.all:
        from core.config import load_enabled_campaigns
        campaigns = load_enabled_campaigns("campaigns")
        if not campaigns:
            log.warning("No enabled campaigns found; nothing to run")
            return
        for cfg in campaigns:
            try:
                run_campaign(cfg.name)
            except SystemExit:
                log.error("Campaign run exited", extra={"campaign": cfg.name})
    else:
        run_campaign(args.campaign)


if __name__ == "__main__":
    main()
