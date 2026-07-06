"""
core/llm.py — Anthropic Messages API client for clip ranking.

Public interface (per ARCHITECTURE §4):
    rank_moments(transcript, rules, comment_summary, clip_len, max_clips) -> list[dict]

The anthropic SDK is imported lazily so that importing this module never
fails in test environments where it is not installed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.settings import get_settings

log = logging.getLogger(__name__)


def _extract_json_array(text: str) -> list[dict] | None:
    """
    Extract the first JSON array from a string, even if surrounded by prose.
    Returns None if no valid array is found.
    """
    # Try to find a JSON array (greedy match of [...])
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        result = json.loads(match.group())
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    return None


def _validate_moments(raw: list[Any], clip_len: tuple[int, int]) -> list[dict]:
    """
    Validate and normalise raw LLM output items.
    Returns only items with required fields and plausible values.
    """
    validated = []
    min_len, max_len = clip_len
    for item in raw:
        if not isinstance(item, dict):
            log.warning("Skipping non-dict moment item", extra={"item": item})
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
            score = float(item["score"])
            hook = str(item.get("hook") or "")
            reason = str(item.get("reason") or "")
        except (KeyError, TypeError, ValueError) as exc:
            log.warning(
                "Skipping malformed moment item",
                extra={"error": str(exc), "item": item},
            )
            continue

        duration = end - start
        if duration < min_len:
            log.debug(
                "Skipping moment shorter than clip_len min",
                extra={"start": start, "end": end, "duration": duration, "min": min_len},
            )
            continue
        if duration > max_len:
            log.debug(
                "Skipping moment longer than clip_len max",
                extra={"start": start, "end": end, "duration": duration, "max": max_len},
            )
            continue
        if not (0.0 <= score <= 1.0):
            log.warning(
                "Score out of [0,1] range; clamping",
                extra={"score": score},
            )
            score = max(0.0, min(1.0, score))

        validated.append(
            {"start": start, "end": end, "score": score, "hook": hook, "reason": reason}
        )

    return validated


def _build_prompt(
    transcript: list[dict],
    rules: str,
    comment_summary: str | None,
    clip_len: tuple[int, int],
    max_clips: int,
) -> str:
    """Build the ranking prompt."""
    seg_lines = "\n".join(
        f"[{seg['start']:.1f}s-{seg['end']:.1f}s] {seg['text']}" for seg in transcript
    )

    comment_section = ""
    if comment_summary:
        comment_section = f"\n\nCOMMENT SIGNAL (top recurring themes from audience):\n{comment_summary}"

    return f"""You are a clip ranking assistant. Analyse the transcript below and identify the best moments to cut as short-form clips.

CAMPAIGN RANKING RULES:
{rules.strip()}

CLIP LENGTH CONSTRAINTS: minimum {clip_len[0]}s, maximum {clip_len[1]}s
MAXIMUM CLIPS TO RETURN: {max_clips}{comment_section}

TRANSCRIPT (timestamps in seconds):
{seg_lines}

Return ONLY a JSON array (no prose, no code fences) of the top moments, best-first, in this exact shape:
[
  {{
    "start": <float seconds>,
    "end": <float seconds>,
    "score": <float 0.0-1.0>,
    "hook": "<one compelling sentence for the first 2 seconds>",
    "reason": "<brief explanation why this moment is strong>"
  }}
]

If no moments meet the criteria, return an empty array: []
"""


def rank_moments(
    transcript: list[dict],
    rules: str,
    comment_summary: str | None,
    clip_len: tuple[int, int],
    max_clips: int,
) -> list[dict]:
    """
    Call the LLM to rank transcript moments.

    Args:
        transcript:      [{start: float, end: float, text: str}]
        rules:           campaign.ranking.ranking_rules text
        comment_summary: optional per-post comment aggregation summary
        clip_len:        (min_seconds, max_seconds)
        max_clips:       max clips to request (LLM hint, enforced again by select_clips)

    Returns:
        [{start, end, score, hook, reason}] — validated, length-filtered.
        May be empty if the LLM finds nothing suitable.

    Raises:
        RuntimeError if LLM_API_KEY or LLM_MODEL are not set.
        Exception on unrecoverable API errors (after one retry).
    """
    try:
        import anthropic  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "anthropic SDK is required for LLM ranking. "
            "Install it with: pip install anthropic"
        ) from exc

    api_key, model = get_settings().require_llm()
    client = anthropic.Anthropic(api_key=api_key)

    prompt = _build_prompt(transcript, rules, comment_summary, clip_len, max_clips)

    def _call() -> str:
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text if message.content else ""

    # First attempt
    response_text = _call()
    log.debug("LLM raw response", extra={"length": len(response_text)})

    moments_raw = _extract_json_array(response_text)

    if moments_raw is None:
        log.warning(
            "LLM response did not contain a parseable JSON array; retrying once",
            extra={"response_preview": response_text[:300]},
        )
        response_text = _call()
        moments_raw = _extract_json_array(response_text)

    if moments_raw is None:
        log.error(
            "LLM failed to return a JSON array after retry; returning empty",
            extra={"response_preview": response_text[:300]},
        )
        return []

    validated = _validate_moments(moments_raw, clip_len)
    log.info(
        "LLM ranking complete",
        extra={"raw_count": len(moments_raw), "validated_count": len(validated)},
    )
    return validated
