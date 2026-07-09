"""
producer/run.py — campaign pipeline entrypoint.

Usage:
    python -m producer.run <campaign_name> [options]
    python -m producer.run --all [options]

Options:
    --mode demo|production  Override campaign cfg.mode for all clips in this run.
    --max-modal-spend X     Abort the render stage if estimated Modal cost > $X.
    --max-apify-spend Y     Abort after discovery if estimated Apify cost > $Y
                            (rough: $0.01/item).
    --dry-run               Run the full pipeline but skip Postiz posting.

Orchestrates SPEC §4 stages 1–5 + download + render + queue:
    1. Discover sources
    2. Dedupe (source level)
    3. Comment signal (TikTok only, optional)
    4. Transcribe selected sources
    5. Rank → select clips (non-overlap, exhaust_source loop)
    6. Download source video
    7. Render clip via configured backend (Modal GPU or local ffmpeg)
    8. Insert clips row status=pending_review (kind/mode/aspect stamped)
    9. Mark source done/partially_done, update used_ranges, clean up raw files

Each source is wrapped in its own try/except so one failure never kills the run.
Structured logging throughout.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Logging must be configured before any other import that uses it
from core.logging import configure_logging

configure_logging()
log = logging.getLogger(__name__)

# Demo runs stop after this many clips (spec §9). Production has no early stop
# here — it is bounded by the spend guard and daily caps instead.
DEMO_CLIP_TARGET = 3


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
    *,
    run_mode: str,
    max_modal_spend: float | None = None,
) -> list[dict]:
    """
    Process a single source through stages 3–8.
    Returns list of inserted clip dicts (may be empty).
    Returns [] on any error (logs the error).

    run_mode: 'demo' or 'production' — stamped on every Clip row.
    max_modal_spend: if set, aborts the render stage for this source when the
        estimated Modal cost would exceed this value.
    """
    from core.models import Source as SourceModel, Clip
    from producer.dedupe import mark_source_status, update_used_ranges
    from producer.comments import pull_and_store_comments
    from producer.transcripts import fetch_and_store_transcript
    from producer.ranker import rank_clips, select_clips
    from producer.download import download_source, cleanup_source
    from producer.render_dispatch import render_and_record, estimate_modal_batch_cost
    from core.storage import work_dir
    from core.settings import get_settings

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

        # ----------------------------------------------------------------
        # Spend guard: check estimated Modal cost before dispatching.
        # ----------------------------------------------------------------
        if max_modal_spend is not None:
            estimated = estimate_modal_batch_cost(len(selected), session)
            if estimated > max_modal_spend:
                log.error(
                    "Modal spend guard triggered: estimated $%.4f for %d clips "
                    "exceeds --max-modal-spend $%.2f — aborting render stage for source %s",
                    estimated, len(selected), max_modal_spend, source_id,
                )
                mark_source_status(session, source_id, "done")
                session.commit()
                return []

        # Warn if MTD spend is >= 80% of monthly budget
        _warn_if_near_monthly_budget(session, source_id)

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
        inserted_clips: list[dict] = []
        new_ranges: list[list[float]] = []
        wdir = work_dir(source_id)

        for clip_candidate in selected:
            try:
                dispatch_result = render_and_record(
                    cfg=campaign_cfg,
                    source_meta=source,
                    clip_candidate=clip_candidate,
                    source_video=Path(source_video_path),
                    words=None,   # Modal worker / local path runs faster-whisper
                    workdir=wdir,
                    campaign_name=campaign_name,
                    campaign_mode=run_mode,
                    session=session,
                )

                if dispatch_result.status == "error":
                    log.error(
                        "Clip render returned error; skipping",
                        extra={
                            "source_id": source_id,
                            "start": clip_candidate.get("start"),
                            "end": clip_candidate.get("end"),
                            "error": dispatch_result.error,
                        },
                    )
                    continue

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
                    # Revamp v2: stamp kind / mode / aspect on every clip row
                    kind="clip",
                    mode=run_mode,
                    aspect="9:16",
                    hook=clip_candidate.get("hook"),
                    score=clip_candidate.get("score"),
                    reason=clip_candidate.get("reason"),
                    file_path=dispatch_result.file_path,
                    thumb_path=dispatch_result.thumb_path,
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
                        "mode": run_mode,
                        "backend": dispatch_result.backend,
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


def _warn_if_near_monthly_budget(session: Any, source_id: str) -> None:
    """Log a warning when month-to-date Modal spend >= 80% of MODAL_MONTHLY_BUDGET."""
    try:
        from producer.render_dispatch import month_to_date_modal_spend
        from core.settings import get_settings
        mtd = month_to_date_modal_spend(session)
        budget = get_settings().modal_monthly_budget
        if budget > 0 and mtd >= budget * 0.8:
            log.warning(
                "MODAL SPEND WARNING: month-to-date estimated spend $%.2f is "
                ">= 80%% of MODAL_MONTHLY_BUDGET ($%.2f). source_id=%s",
                mtd, budget, source_id,
            )
    except Exception:
        pass


def run_campaign(
    campaign_name: str,
    *,
    run_mode: str | None = None,
    max_modal_spend: float | None = None,
    max_apify_spend: float | None = None,
) -> None:
    """Execute a full pipeline run for one campaign.

    run_mode: 'demo' or 'production' — overrides cfg.mode when set.
    max_modal_spend: abort render stage if estimated Modal cost exceeds this USD value.
    max_apify_spend: abort after discovery if estimated Apify cost exceeds this USD value.
        Rough rate: $0.01 per discovered item (documented approximation).
    """
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
    from producer.render_dispatch import APIFY_COST_PER_ITEM

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

    # Determine the effective mode for this run
    effective_mode = run_mode if run_mode is not None else campaign_cfg.mode
    log.info(
        "Campaign run starting",
        extra={"campaign": campaign_name, "mode": effective_mode},
    )

    apify = Apify()

    run_start = datetime.now(tz=timezone.utc)

    # Stage 1: discover
    candidates = discover_all(campaign_cfg, apify)
    if not candidates:
        log.warning("No candidates discovered", extra={"campaign": campaign_name})
        return

    # Apify spend guard: rough estimate of cost for this discovery batch.
    # Rate: $0.01 per discovered item (approximation documented in render_dispatch.py).
    if max_apify_spend is not None:
        apify_estimate = len(candidates) * APIFY_COST_PER_ITEM
        if apify_estimate > max_apify_spend:
            log.error(
                "Apify spend guard triggered: discovered %d items estimated at "
                "$%.2f exceeds --max-apify-spend $%.2f — aborting run for %s",
                len(candidates), apify_estimate, max_apify_spend, campaign_name,
            )
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

    cpu_count = os.cpu_count() or 2

    # Demo runs target a small number of clips (spec §9: "produce 3 clips";
    # "don't hunt for 100"). Stop processing further sources once we have enough,
    # so a demo doesn't grind transcripts for every discovered candidate.
    clip_target = DEMO_CLIP_TARGET if effective_mode == "demo" else None

    total_clips = 0
    sources_processed = 0
    for source in to_process:
        # Each source gets its own session to isolate failures
        with get_session() as session:
            clips = _process_source(
                source,
                campaign_cfg,
                apify,
                session,
                cpu_count,
                run_mode=effective_mode,
                max_modal_spend=max_modal_spend,
            )
            total_clips += len(clips)
            sources_processed += 1

        if clip_target is not None and total_clips >= clip_target:
            log.info(
                "Demo clip target reached; stopping source processing",
                extra={"campaign": campaign_name, "clips": total_clips,
                       "target": clip_target},
            )
            break

    run_end = datetime.now(tz=timezone.utc)
    elapsed = (run_end - run_start).total_seconds()
    log.info(
        "Campaign run complete",
        extra={
            "campaign": campaign_name,
            "sources_processed": sources_processed,
            "clips_queued": total_clips,
            "elapsed_sec": round(elapsed, 1),
            "mode": effective_mode,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clip Engine producer — run a campaign pipeline"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("campaign", nargs="?", help="Campaign name (filename without .yaml)")
    group.add_argument("--all", action="store_true", help="Run all enabled campaigns")

    parser.add_argument(
        "--mode",
        choices=["demo", "production"],
        default=None,
        help="Override campaign cfg.mode for all clips in this run",
    )
    parser.add_argument(
        "--max-modal-spend",
        type=float,
        default=None,
        metavar="USD",
        help="Abort render stage if estimated Modal cost exceeds this amount (USD)",
    )
    parser.add_argument(
        "--max-apify-spend",
        type=float,
        default=None,
        metavar="USD",
        help="Abort after discovery if estimated Apify cost exceeds this amount (USD). "
             "Rough rate: $0.01/item.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but skip Postiz posting (passed to scheduler)",
    )
    args = parser.parse_args()

    run_kwargs = dict(
        run_mode=args.mode,
        max_modal_spend=args.max_modal_spend,
        max_apify_spend=args.max_apify_spend,
    )

    if args.all:
        from core.config import load_enabled_campaigns
        campaigns = load_enabled_campaigns("campaigns")
        if not campaigns:
            log.warning("No enabled campaigns found; nothing to run")
            return
        for cfg in campaigns:
            try:
                run_campaign(cfg.name, **run_kwargs)
            except SystemExit:
                log.error("Campaign run exited", extra={"campaign": cfg.name})
    else:
        run_campaign(args.campaign, **run_kwargs)


if __name__ == "__main__":
    main()
