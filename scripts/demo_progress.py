"""
scripts/demo_progress.py — Demo pipeline progress replay.

Replays tests/fixtures/progress/demo_events.json into a local SQLite DB at
realistic pacing so a locally-running API serves live SSE events to the real
UI — zero spend, zero network, zero GPU.

Usage:
    .venv/bin/python scripts/demo_progress.py [--fast]
    make demo-progress

Options:
    --fast      Emit all events instantly (no sleep); useful for CI sanity check.

Pacing (default):
    Each simulated second costs (1 / SPEED_FACTOR) real seconds.
    SPEED_FACTOR=8 → a 147-second simulated run takes ~18 real seconds to emit.
    The UI progress bar moves in real-time; the full demo plays in ~2-3 minutes
    if SPEED_FACTOR is set lower (e.g. 1) or near-instant with --fast.

At startup, prints instructions for booting uvicorn against the same DB.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root on sys.path
# ---------------------------------------------------------------------------

_REPO = Path(__file__).parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_FIXTURE = _REPO / "tests" / "fixtures" / "progress" / "demo_events.json"
_DB_PATH = _REPO / "demo_progress.db"
_DB_URL = f"sqlite:///{_DB_PATH}"

# 1 simulated second = 1/SPEED_FACTOR real seconds.
# Default 8: a 147 s sim run takes ~18 real seconds; you see smooth progress.
# Set to 1 for "real time" (2.5 min); --fast overrides to instant.
SPEED_FACTOR: int = 8


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _init_db() -> None:
    """Reset and create the demo SQLite DB."""
    import core.db as _cdb
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.models import Base

    # Wipe any previous demo run
    if _DB_PATH.exists():
        _DB_PATH.unlink()

    engine = create_engine(_DB_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    # Wire the engine into core.db so the API (if running) picks it up too
    _cdb._engine = engine  # type: ignore[attr-defined]
    _cdb._SessionLocal = sessionmaker(  # type: ignore[attr-defined]
        bind=engine, autocommit=False, autoflush=False, expire_on_commit=False
    )


def _insert_source_and_clips(fixture: dict) -> list[int]:
    """Insert Campaign + Source + Clip rows from the fixture.

    Returns list of actual DB clip IDs in insertion order.
    """
    from core.db import get_session
    from core.models import Campaign, Source, Clip

    clip_ids: list[int] = []

    src_data = fixture["source"]
    clips_data = fixture["clips"]

    with get_session() as session:
        # Campaign
        session.add(Campaign(name=src_data["campaign"], enabled=True))
        session.flush()

        # Source
        source = Source(
            source_id=src_data["source_id"],
            campaign=src_data["campaign"],
            platform=src_data["platform"],
            url=src_data["url"],
            title=src_data.get("title"),
            author_handle=src_data.get("author_handle"),
            status=src_data.get("status", "pending"),
            stage=src_data.get("stage", "queued"),
        )
        session.add(source)
        session.flush()

        # Clips — inserted in order; IDs will be 1, 2, 3, 4 on a fresh DB
        for cd in clips_data:
            clip = Clip(
                source_id=cd["source_id"],
                campaign=cd["campaign"],
                start=cd.get("start"),
                end=cd.get("end"),
                hook=cd.get("hook"),
                score=cd.get("score"),
                kind=cd.get("kind", "clip"),
                mode=cd.get("mode", "demo"),
                aspect=cd.get("aspect", "9:16"),
                gate_status=cd.get("gate_status", "pending"),
                status=cd.get("status", "pending_review"),
                file_path="",
                thumb_path="",
                caption=cd.get("hook", ""),
                destination_channels=[],
                correction_attempts=0,
                critic_reports=[],
            )
            session.add(clip)
            session.flush()
            clip_ids.append(clip.id)

        session.commit()

    return clip_ids


# ---------------------------------------------------------------------------
# Event replay
# ---------------------------------------------------------------------------


def _resolve_clip_id(fixture_clip_id: int | None, id_map: dict[int, int]) -> int | None:
    """Map fixture clip_id (1-based) to actual DB clip ID."""
    if fixture_clip_id is None:
        return None
    return id_map.get(fixture_clip_id, fixture_clip_id)


def _replay_events(
    fixture: dict,
    id_map: dict[int, int],
    fast: bool,
) -> None:
    """Insert PipelineEvent rows from the fixture at realistic pacing.

    Updates clip gate_status when terminal events (ready/didnt_pass) are replayed,
    and updates source.stage as each major stage is entered.
    """
    from core.db import get_session
    from core.models import PipelineEvent, Clip, Source

    events = fixture["events"]
    source_id = fixture["source"]["source_id"]
    total = len(events)

    # Track which clip gate_statuses we've already set (avoid re-setting)
    clip_terminal_set: set[int] = set()

    real_start = time.monotonic()

    for i, ev in enumerate(events):
        t_offset = float(ev.get("t_offset_s", 0))

        if not fast:
            target_real = t_offset / SPEED_FACTOR
            elapsed = time.monotonic() - real_start
            sleep_s = target_real - elapsed
            if sleep_s > 0:
                time.sleep(sleep_s)

        clip_id = _resolve_clip_id(ev.get("clip_id"), id_map)
        created_at = datetime.now(tz=timezone.utc)

        with get_session() as session:
            row = PipelineEvent(
                source_id=source_id,
                clip_id=clip_id,
                stage=ev["stage"],
                status=ev["status"],
                progress_n=ev.get("n"),
                progress_total=ev.get("total"),
                detail=ev.get("detail") or "",
                reason=ev.get("reason"),
                created_at=created_at,
            )
            session.add(row)

            # Update source.stage to reflect current pipeline stage
            source_stage = ev["stage"]
            if source_stage not in ("judging", "identified", "correction",
                                    "ready", "didnt_pass"):
                # Only update for "major" source-level stages
                try:
                    src = session.query(Source).filter_by(source_id=source_id).first()
                    if src and source_stage in (
                        "queued", "transcribing", "downloading", "identifying",
                        "rendering", "reviewing", "complete",
                    ):
                        src.stage = source_stage
                except Exception:
                    pass

            # Update clip gate_status on terminal events
            if clip_id is not None and ev["stage"] in ("ready", "didnt_pass"):
                if clip_id not in clip_terminal_set:
                    clip_terminal_set.add(clip_id)
                    try:
                        clip = session.query(Clip).filter_by(id=clip_id).first()
                        if clip:
                            clip.gate_status = ev["stage"]
                            if ev["stage"] == "didnt_pass":
                                clip.status = "rejected"
                    except Exception:
                        pass

            session.commit()

        pct = int((i + 1) / total * 100)
        print(
            f"\r  [{pct:3d}%] event {i + 1}/{total}: "
            f"{ev['stage']}:{ev['status']}"
            + (f" (clip {clip_id})" if clip_id else ""),
            end="",
            flush=True,
        )

    print()  # newline after progress bar


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clip Engine — demo pipeline progress replay (zero spend)"
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Emit all events instantly (no sleep delays)",
    )
    args = parser.parse_args(argv)

    # Load fixture
    if not _FIXTURE.exists():
        print(f"ERROR: fixture not found: {_FIXTURE}", file=sys.stderr)
        return 1

    with _FIXTURE.open(encoding="utf-8") as fh:
        fixture = json.load(fh)

    source_id = fixture["source"]["source_id"]
    n_events = len(fixture["events"])
    n_clips = len(fixture["clips"])

    # ── Print instructions ────────────────────────────────────────────────
    print("=" * 72)
    print("CLIP ENGINE — DEMO PIPELINE PROGRESS REPLAY")
    print("=" * 72)
    print()
    print("This script replays a pre-recorded pipeline run into a local SQLite DB")
    print("so you can demo the live SSE progress UI with zero spend.")
    print()
    print("STEP 1 — Boot the local API (in a separate terminal):")
    print()
    print(f"    DATABASE_URL='{_DB_URL}' \\")
    print(f"    WEB_ADMIN_PASSWORD=demo \\")
    print(f"    .venv/bin/uvicorn web.api:app --reload --port 8000")
    print()
    print("STEP 2 — Open the UI in your browser:")
    print()
    print("    http://localhost:8000")
    print()
    print(f"STEP 3 — Watch the Sources tab for source: {source_id}")
    print()
    print("-" * 72)
    print()
    print(
        f"Fixture: {n_events} events · {n_clips} clips"
        f" · source {source_id}"
    )
    if args.fast:
        print("Mode: --fast (instant, no delays)")
    else:
        pacing_s = fixture["events"][-1].get("t_offset_s", 0)
        real_s = pacing_s / SPEED_FACTOR
        print(
            f"Mode: paced (SPEED_FACTOR={SPEED_FACTOR}; "
            f"~{real_s:.0f}s real time for {pacing_s}s simulated)"
        )
    print()

    # ── Inject DB env + settings ──────────────────────────────────────────
    os.environ["DATABASE_URL"] = _DB_URL
    os.environ.setdefault("WEB_ADMIN_PASSWORD", "demo")

    try:
        from core.settings import get_settings
        get_settings.cache_clear()
    except Exception:
        pass

    # ── Init DB ──────────────────────────────────────────────────────────
    print(f"Initialising demo DB: {_DB_PATH}")
    _init_db()

    # ── Insert source + clips ─────────────────────────────────────────────
    print("Inserting source and clip rows...")
    clip_ids = _insert_source_and_clips(fixture)

    # Build fixture clip_id → actual DB clip_id map
    id_map: dict[int, int] = {}
    for fixture_clip, actual_id in zip(fixture["clips"], clip_ids):
        id_map[fixture_clip["id"]] = actual_id
    print(f"Clips inserted with IDs: {clip_ids}")
    print()

    # ── Replay events ─────────────────────────────────────────────────────
    print(f"Replaying {n_events} events...")
    _replay_events(fixture, id_map, fast=args.fast)

    print()
    print("=" * 72)
    print("Replay complete.")
    print()
    print(f"The demo DB is at: {_DB_PATH}")
    print("If the API is running, the Sources tab already shows the result.")
    print()
    print("To replay again:")
    print(f"    .venv/bin/python {Path(__file__).relative_to(_REPO)} [--fast]")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
