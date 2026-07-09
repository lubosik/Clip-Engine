"""
meme/run.py — meme engine CLI entrypoint.

Usage:
    python -m meme.run <campaign>
    python -m meme.run --all
    python -m meme.run <campaign> --n 4 --mode demo
    python -m meme.run <campaign> --text-only --n 2

Loads enabled campaigns, checks engines.memes is True, ensures a profile
exists (extract on first run; politely refuses if refs_dir is empty),
then generates n memes.

Mirrors producer/run.py in CLI style, logging, and session handling.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.logging import configure_logging

configure_logging()
log = logging.getLogger(__name__)


def run_meme_campaign(
    campaign_name: str,
    *,
    n: int = 4,
    mode_override: str | None = None,
    text_only: bool = False,
) -> None:
    """Execute the meme generation pipeline for one campaign."""
    from core.config import load_campaign
    from core.db import get_session

    campaign_path = Path("campaigns") / f"{campaign_name}.yaml"
    if not campaign_path.exists():
        log.error(
            "Campaign YAML not found",
            extra={"campaign": campaign_name, "path": str(campaign_path)},
        )
        sys.exit(1)

    try:
        campaign_cfg = load_campaign(campaign_path, strict_assets=False)
    except (FileNotFoundError, ValueError) as exc:
        log.error(
            "Campaign config failed",
            extra={"campaign": campaign_name, "error": str(exc)},
        )
        sys.exit(1)

    if not campaign_cfg.enabled:
        log.info(
            "Campaign is disabled; skipping",
            extra={"campaign": campaign_name},
        )
        return

    if not campaign_cfg.engines.memes:
        log.info(
            "engines.memes is false for campaign '%s'; "
            "enable it in the YAML or via the dashboard toggle to run the meme engine",
            campaign_name,
        )
        return

    if not campaign_cfg.meme or not campaign_cfg.meme.refs_dir:
        log.error(
            "meme.refs_dir is not configured for campaign '%s'. "
            "Set meme.refs_dir in the YAML and drop reference images there.",
            campaign_name,
        )
        sys.exit(1)

    run_start = datetime.now(tz=timezone.utc)
    log.info(
        "Meme run starting",
        extra={
            "campaign": campaign_name,
            "n": n,
            "mode_override": mode_override,
            "text_only": text_only,
            "run_start": run_start.isoformat(),
        },
    )

    with get_session() as session:
        if text_only:
            from meme.text_posts import generate_text_posts

            inserted_ids = generate_text_posts(
                campaign_cfg,
                n,
                session,
                mode_override=mode_override,
            )
            session.commit()
        else:
            from meme.generate import generate_memes

            inserted_ids = generate_memes(
                campaign_cfg,
                n,
                session,
                mode_override=mode_override,
            )
            session.commit()

    run_end = datetime.now(tz=timezone.utc)
    elapsed = (run_end - run_start).total_seconds()
    log.info(
        "Meme run complete",
        extra={
            "campaign": campaign_name,
            "memes_queued": len(inserted_ids),
            "elapsed_sec": round(elapsed, 1),
            "clip_ids": inserted_ids,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clip Engine meme engine — generate memes for a campaign"
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "campaign",
        nargs="?",
        help="Campaign name (filename without .yaml)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Run all enabled campaigns that have engines.memes enabled",
    )

    parser.add_argument(
        "--n",
        type=int,
        default=4,
        help="Number of memes (or text posts) to generate per campaign (default: 4)",
    )
    parser.add_argument(
        "--mode",
        choices=["demo", "production"],
        default=None,
        help="Override campaign mode (demo or production)",
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="Generate text/X posts instead of image memes",
    )

    args = parser.parse_args()

    if args.all:
        from core.config import load_enabled_campaigns

        campaigns = load_enabled_campaigns("campaigns")
        if not campaigns:
            log.warning("No enabled campaigns found; nothing to run")
            return

        for cfg in campaigns:
            if not cfg.engines.memes:
                log.debug(
                    "Skipping campaign '%s': engines.memes is false", cfg.name
                )
                continue
            try:
                run_meme_campaign(
                    cfg.name,
                    n=args.n,
                    mode_override=args.mode,
                    text_only=args.text_only,
                )
            except SystemExit:
                log.error("Meme run exited", extra={"campaign": cfg.name})
    else:
        run_meme_campaign(
            args.campaign,
            n=args.n,
            mode_override=args.mode,
            text_only=args.text_only,
        )


if __name__ == "__main__":
    main()
