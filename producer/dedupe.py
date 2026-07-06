"""
producer/dedupe.py — source deduplication logic.

Pure utility functions:
    compute_source_id(platform, native_id) -> str
    is_duplicate(source_id, existing_ids) -> bool
    filter_new_candidates(candidates, existing_ids) -> list[dict]
    sort_by_engagement(candidates) -> list[dict]

DB-bound functions (require a SQLAlchemy session):
    get_existing_source_ids(session, campaign) -> set[str]
    upsert_source(session, candidate, campaign) -> Source
    mark_source_status(session, source_id, status) -> None
    update_used_ranges(session, source_id, new_ranges) -> None
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from core.models import Source


# ---------------------------------------------------------------------------
# Pure functions (no DB, no network — safe in tests)
# ---------------------------------------------------------------------------

def compute_source_id(platform: str, native_id: str) -> str:
    """
    Compute the stable, globally unique source identifier.

    Args:
        platform:  "youtube" | "tiktok" | "instagram"
        native_id: the platform's native video/post id

    Returns:
        "{platform}:{native_id}"
    """
    if not platform:
        raise ValueError("platform must not be empty")
    if not native_id:
        raise ValueError("native_id must not be empty")
    return f"{platform}:{native_id}"


def is_duplicate(source_id: str, existing_ids: set[str]) -> bool:
    """Return True if source_id is already in existing_ids."""
    return source_id in existing_ids


def filter_new_candidates(
    candidates: list[dict[str, Any]],
    existing_ids: set[str],
) -> list[dict[str, Any]]:
    """
    Return only candidates whose source_id is not in existing_ids.

    Expects each candidate to have a "source_id" key (pre-computed).
    """
    result = []
    skipped = 0
    for c in candidates:
        sid = c.get("source_id")
        if sid is None:
            log.warning("Candidate missing source_id; skipping", extra={"candidate": c})
            skipped += 1
            continue
        if sid in existing_ids:
            log.debug("Skipping duplicate source", extra={"source_id": sid})
            skipped += 1
        else:
            result.append(c)

    log.info(
        "Dedupe filter complete",
        extra={"total": len(candidates), "new": len(result), "skipped": skipped},
    )
    return result


def filter_done_sources(
    candidates: list[dict[str, Any]],
    done_ids: set[str],
) -> list[dict[str, Any]]:
    """
    Return only candidates whose source_id does NOT have status=done.
    This is a separate pass from new-source dedup because partially_done
    sources should still be processed.
    """
    result = [c for c in candidates if c.get("source_id") not in done_ids]
    skipped = len(candidates) - len(result)
    if skipped:
        log.info(
            "Skipped sources with status=done",
            extra={"skipped": skipped, "remaining": len(result)},
        )
    return result


def sort_by_engagement(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Sort candidates by view_count descending (best engagement first).
    Used to prioritise which sources to spend transcript budget on.
    """
    return sorted(candidates, key=lambda c: c.get("view_count", 0), reverse=True)


# ---------------------------------------------------------------------------
# DB-bound functions
# ---------------------------------------------------------------------------

def get_existing_source_ids(session: "Session", campaign: str) -> set[str]:
    """
    Return the set of source_ids already known for this campaign.
    """
    from core.models import Source  # local to avoid circular at module level
    rows = (
        session.query(Source.source_id)
        .filter(Source.campaign == campaign)
        .all()
    )
    return {row.source_id for row in rows}


def get_done_source_ids(session: "Session", campaign: str) -> set[str]:
    """Return source_ids with status='done' for this campaign."""
    from core.models import Source
    rows = (
        session.query(Source.source_id)
        .filter(Source.campaign == campaign, Source.status == "done")
        .all()
    )
    return {row.source_id for row in rows}


def upsert_source(
    session: "Session",
    candidate: dict[str, Any],
    campaign: str,
) -> "Source":
    """
    Insert a new source row or return the existing one.

    candidate must contain: source_id, platform, url.
    Optional: title, author_handle, metadata (raw dict).

    Returns the Source ORM object (not yet committed — caller commits).
    """
    from core.models import Source

    source_id = candidate["source_id"]
    existing = session.query(Source).filter_by(source_id=source_id).first()
    if existing:
        return existing

    source = Source(
        source_id=source_id,
        campaign=campaign,
        platform=candidate["platform"],
        url=candidate["url"],
        title=candidate.get("title"),
        author_handle=candidate.get("author_handle"),
        source_metadata=candidate.get("raw"),
        status="pending",
        used_ranges=[],
    )
    session.add(source)
    log.info(
        "Inserted new source",
        extra={"source_id": source_id, "platform": candidate["platform"]},
    )
    return source


def mark_source_status(
    session: "Session",
    source_id: str,
    status: str,
) -> None:
    """Update source status. Valid values: pending|selected|done|partially_done."""
    valid = {"pending", "selected", "done", "partially_done"}
    if status not in valid:
        raise ValueError(f"Invalid source status {status!r}; must be one of {valid}")

    from core.models import Source
    source = session.query(Source).filter_by(source_id=source_id).first()
    if source is None:
        log.warning("mark_source_status: source not found", extra={"source_id": source_id})
        return
    source.status = status
    source.processed_at = datetime.now(tz=timezone.utc)
    log.info("Updated source status", extra={"source_id": source_id, "status": status})


def update_used_ranges(
    session: "Session",
    source_id: str,
    new_ranges: list[list[float]],
) -> None:
    """
    Append new_ranges to source.used_ranges.
    new_ranges: [[start, end], ...] float seconds.
    """
    from core.models import Source
    source = session.query(Source).filter_by(source_id=source_id).first()
    if source is None:
        log.warning("update_used_ranges: source not found", extra={"source_id": source_id})
        return
    existing = source.used_ranges or []
    source.used_ranges = existing + new_ranges
    log.info(
        "Updated used_ranges",
        extra={
            "source_id": source_id,
            "added": len(new_ranges),
            "total": len(source.used_ranges),
        },
    )
