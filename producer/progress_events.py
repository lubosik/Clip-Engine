"""
producer/progress_events.py — pipeline progress event emitter.

Contract: docs/PROGRESS_EVENTS_CONTRACTS.md §2.

Public API:
    emit_event(session, source_id, stage, *, status, clip_id, n, total,
               detail, reason) -> None
        Inserts a pipeline_events row.  NEVER raises — log+swallow on any
        error.  Caller owns the commit (emit inside the same transaction as
        the state change it describes).

    to_wire(row) -> dict
        Produces the exact v1 JSON shape from a PipelineEvent ORM row (or any
        object with the same attributes).  Used by the SSE sender.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage vocabulary (must match §2)
# ---------------------------------------------------------------------------

VALID_STAGES = frozenset({
    "queued",
    "transcribing",
    "downloading",
    "identifying",
    "identified",
    "pre_verify",
    "rendering",
    "reviewing",
    "correction",
    "judging",
    "ready",
    "didnt_pass",
    "complete",
})

VALID_STATUSES = frozenset({"running", "done", "failed", "corrected"})


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

def emit_event(
    session: Any,
    source_id: str,
    stage: str,
    *,
    status: str = "running",
    clip_id: int | None = None,
    n: int | None = None,
    total: int | None = None,
    detail: str = "",
    reason: str | None = None,
) -> None:
    """Insert a pipeline_events row.

    Args:
        session:   SQLAlchemy session (caller owns commit).
        source_id: "{platform}:{id}" string — must exist in sources table (FK).
        stage:     Stage vocabulary string from PROGRESS_EVENTS_CONTRACTS §2.
        status:    "running" | "done" | "failed" | "corrected"
        clip_id:   FK to clips.id when the event is per-clip; None for source-level.
        n:         progress_n (e.g. clip index in the batch).
        total:     progress_total (e.g. total clips).
        detail:    Human-readable one-liner shown verbatim in the live UI.
        reason:    Failure/correction explanation (None for non-failure events).

    Contract:
        NEVER raises.  Any error is logged and swallowed so no pipeline stage
        is killed by a progress-tracking failure.
    """
    try:
        from core.models import PipelineEvent

        row = PipelineEvent(
            source_id=source_id,
            clip_id=clip_id,
            stage=stage,
            status=status,
            progress_n=n,
            progress_total=total,
            detail=detail or "",
            reason=reason,
            created_at=datetime.now(tz=timezone.utc),
        )
        session.add(row)
        log.debug(
            "emit_event: source_id=%s stage=%s status=%s clip_id=%s detail=%r",
            source_id, stage, status, clip_id, (detail or "")[:80],
        )
    except Exception as exc:
        log.warning(
            "emit_event failed (non-fatal): source_id=%s stage=%s: %s",
            source_id, stage, exc,
        )


# ---------------------------------------------------------------------------
# Wire serialiser
# ---------------------------------------------------------------------------

def to_wire(row: Any) -> dict:
    """Produce the v1 SSE data JSON from a PipelineEvent row.

    Shape per §2:
    {
      "v": 1,
      "source_id": "youtube:abc",
      "ts": "<iso8601>",
      "stage": "rendering",
      "clip_id": 141,
      "progress": {"n": 3, "total": 10},    # omitted when both n and total are null
      "status": "running",
      "detail": "Creating clip 3 of 10 — rendering on Modal",
      "reason": null
    }
    """
    created_at = getattr(row, "created_at", None)
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        ts = created_at.isoformat()
    else:
        ts = datetime.now(tz=timezone.utc).isoformat()

    n = getattr(row, "progress_n", None)
    total = getattr(row, "progress_total", None)
    progress: dict | None = None
    if n is not None or total is not None:
        progress = {"n": n, "total": total}

    return {
        "v": 1,
        "source_id": getattr(row, "source_id", ""),
        "ts": ts,
        "stage": getattr(row, "stage", ""),
        "clip_id": getattr(row, "clip_id", None),
        "progress": progress,
        "status": getattr(row, "status", "running"),
        "detail": getattr(row, "detail", "") or "",
        "reason": getattr(row, "reason", None),
    }
