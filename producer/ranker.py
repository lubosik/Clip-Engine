"""
producer/ranker.py — clip ranking and selection.

Public interface (per ARCHITECTURE §4):
    select_clips(candidates, used_ranges, cfg) -> list[dict]

select_clips is a PURE function — no I/O, no DB, no network.
It enforces: min_score, clip_length min/max, non-overlap vs used_ranges
AND vs previously accepted clips this call, max_clips_per_source.

rank_clips wraps core.llm.rank_moments and is called by run.py.
exhaust_source looping is the CALLER's responsibility (run.py).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import RankingConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Overlap helpers (pure)
# ---------------------------------------------------------------------------

def _ranges_overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    """Return True if [a_start, a_end) overlaps with [b_start, b_end)."""
    return a_start < b_end and b_start < a_end


def _overlaps_any(
    start: float,
    end: float,
    ranges: list[list[float]],
) -> bool:
    """Return True if [start, end) overlaps with any range in `ranges`."""
    for r in ranges:
        if _ranges_overlap(start, end, r[0], r[1]):
            return True
    return False


# ---------------------------------------------------------------------------
# Public pure function
# ---------------------------------------------------------------------------

def select_clips(
    candidates: list[dict],
    used_ranges: list[list[float]],
    cfg: "RankingConfig",
) -> list[dict]:
    """
    Select the best non-overlapping clips from LLM candidates.

    Pure function — no side effects, no I/O.

    Args:
        candidates:   Output of rank_moments: [{start, end, score, hook, reason}]
        used_ranges:  Already-used time ranges from previous runs: [[start, end], ...]
        cfg:          RankingConfig — provides clip_length, min_score,
                      max_clips_per_source.

    Returns:
        Accepted clip dicts (same shape as candidates), sorted by score desc.
        Never exceeds max_clips_per_source.

    Selection algorithm:
        1. Filter by score >= min_score
        2. Filter by duration within clip_length [min, max]
        3. Sort by score descending (greedy best-first)
        4. Accept greedily: reject any candidate that overlaps with used_ranges
           OR with any already-accepted candidate this call
        5. Stop once max_clips_per_source is reached
    """
    min_len, max_len = cfg.clip_length[0], cfg.clip_length[1]
    min_score = cfg.min_score
    cap = cfg.max_clips_per_source

    # Step 1 & 2: filter
    eligible = []
    for c in candidates:
        score = c.get("score", 0.0)
        start = c.get("start", 0.0)
        end = c.get("end", 0.0)
        duration = end - start

        if score < min_score:
            log.debug(
                "Candidate rejected: score below min",
                extra={"start": start, "end": end, "score": score, "min_score": min_score},
            )
            continue
        if duration < min_len:
            log.debug(
                "Candidate rejected: too short",
                extra={"start": start, "end": end, "duration": duration, "min_len": min_len},
            )
            continue
        if duration > max_len:
            log.debug(
                "Candidate rejected: too long",
                extra={"start": start, "end": end, "duration": duration, "max_len": max_len},
            )
            continue

        eligible.append(c)

    # Step 3: sort by score descending
    eligible.sort(key=lambda c: c.get("score", 0.0), reverse=True)

    # Step 4 & 5: greedy accept
    accepted: list[dict] = []
    # Accumulate accepted ranges to check mutual non-overlap
    accepted_ranges: list[list[float]] = []

    for c in eligible:
        if len(accepted) >= cap:
            log.debug(
                "Reached max_clips_per_source cap",
                extra={"cap": cap},
            )
            break

        start = c["start"]
        end = c["end"]

        # Check against historical used_ranges
        if _overlaps_any(start, end, used_ranges):
            log.debug(
                "Candidate rejected: overlaps used_ranges",
                extra={"start": start, "end": end},
            )
            continue

        # Check against already-accepted clips in this call
        if _overlaps_any(start, end, accepted_ranges):
            log.debug(
                "Candidate rejected: overlaps accepted clip in this run",
                extra={"start": start, "end": end},
            )
            continue

        accepted.append(c)
        accepted_ranges.append([start, end])

    log.info(
        "select_clips complete",
        extra={
            "candidates": len(candidates),
            "eligible": len(eligible),
            "accepted": len(accepted),
            "cap": cap,
            "used_ranges_count": len(used_ranges),
        },
    )
    return accepted


# ---------------------------------------------------------------------------
# LLM-calling wrapper (not pure — calls core.llm)
# ---------------------------------------------------------------------------

def rank_clips(
    transcript: list[dict],
    comment_summary: str | None,
    cfg: "RankingConfig",
) -> list[dict]:
    """
    Call the LLM to rank transcript moments.

    Args:
        transcript:      [{start, end, text}]
        comment_summary: optional comment signal string
        cfg:             RankingConfig

    Returns:
        LLM-ranked candidates [{start, end, score, hook, reason}]
        (not yet overlap-filtered; pass to select_clips).
    """
    from core.llm import rank_moments  # lazy import guards against missing anthropic SDK

    return rank_moments(
        transcript=transcript,
        rules=cfg.ranking_rules,
        comment_summary=comment_summary,
        clip_len=(cfg.clip_length[0], cfg.clip_length[1]),
        max_clips=cfg.max_clips_per_source,
    )
