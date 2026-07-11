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

from core.sentences import build_sentence_spans, snap_to_sentences
from core.settings import get_settings
from core.topics import (
    FEWSHOT_BOUNDARY_EXAMPLES,
    segment_transcript,
    snap_end_off_next_topic,
)

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
    topic_segments: list[dict] | None = None,
) -> str:
    """Build the ranking prompt."""
    seg_lines = "\n".join(
        f"[{seg['start']:.1f}s-{seg['end']:.1f}s] {seg['text']}" for seg in transcript
    )

    comment_section = ""
    if comment_summary:
        comment_section = f"\n\nCOMMENT SIGNAL (top recurring themes from audience):\n{comment_summary}"

    # If a segmentation pass ran, offer the topic segments as the candidate pool
    # so the model selects clips from complete topics rather than by the clock.
    topic_section = ""
    if topic_segments:
        topic_lines = "\n".join(
            f"[{t['start']:.1f}s-{t['end']:.1f}s] {t.get('summary', '')}"
            + (f"  (ends because: {t['ends_because']})" if t.get("ends_because") else "")
            for t in topic_segments
        )
        topic_section = (
            "\n\nTOPIC SEGMENTS (a segmentation pass already split this transcript into "
            "self-contained topics — each is ONE complete idea; prefer choosing clip "
            "boundaries that align to these segments, and NEVER let a clip's end cross "
            "into the start of the following segment):\n"
            f"{topic_lines}"
        )

    return f"""You are a clip ranking assistant. Analyse the transcript below and identify the best moments to cut as short-form clips.

CAMPAIGN RANKING RULES:
{rules.strip()}

CLIP LENGTH CONSTRAINTS: minimum {clip_len[0]}s, maximum {clip_len[1]}s
MAXIMUM CLIPS TO RETURN: {max_clips}{comment_section}

SENTENCE-BOUNDARY RULE (mandatory): Choose start at the FIRST word of a sentence \
and end at the LAST word of a sentence — the clip must be a complete, coherent \
thought that does not start or end mid-sentence. The hook must describe the point \
made in the opening sentence.

TOPIC-BOUNDARY RULE (mandatory): One clip = one complete idea. Start where a \
self-contained thought begins and END where that thought RESOLVES — right before \
a topic change, a new question from the host, or the speaker moving to a different \
subject. NEVER end a clip on the first sentence of a new topic; if a new subject \
has only just been introduced at the tail, that subject belongs to the NEXT clip, \
so trim the end back to where the prior thought completed. Topic completeness wins \
over hitting a target length.

{FEWSHOT_BOUNDARY_EXAMPLES}
{topic_section}

TRANSCRIPT (timestamps in seconds):
{seg_lines}

Return ONLY a JSON array (no prose, no code fences) of the top moments, best-first, in this exact shape:
[
  {{
    "start": <float seconds — must be the first word of a sentence>,
    "end": <float seconds — must be the last word of a sentence>,
    "score": <float 0.0-1.0>,
    "hook": "<one compelling sentence summarising the opening sentence of this clip>",
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

    settings = get_settings()
    api_key, model = settings.require_llm()

    # Route by provider: explicit LLM_BASE_URL wins; otherwise OpenRouter keys
    # go to OpenRouter's Anthropic-compatible endpoint, Anthropic keys go direct.
    base_url = settings.llm_base_url
    if base_url is None and api_key.startswith("sk-or-"):
        base_url = "https://openrouter.ai/api"
    if base_url:
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        log.info("LLM client using base_url=%s model=%s", base_url, model)
    else:
        client = anthropic.Anthropic(api_key=api_key)

    # Segmentation pass: split the transcript into self-contained topic segments
    # BEFORE selection so clip boundaries are chosen semantically, not by clock.
    # Best-effort — returns [] on any failure and the ranker proceeds without it.
    topic_segments = segment_transcript(transcript, clip_len)
    if topic_segments:
        log.info("Topic segmentation produced %d segments", len(topic_segments))

    prompt = _build_prompt(
        transcript, rules, comment_summary, clip_len, max_clips, topic_segments
    )

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

    # ------------------------------------------------------------------
    # Snap every moment's start/end to whole-sentence boundaries so that
    # clips never begin or end mid-word / mid-thought.
    # This is a best-effort post-process: if it fails for any reason the
    # raw validated timestamps are used unchanged.
    # ------------------------------------------------------------------
    try:
        sentence_spans = build_sentence_spans(transcript)
        if sentence_spans:
            snapped: list[dict] = []
            for moment in validated:
                new_start, new_end = snap_to_sentences(
                    moment["start"],
                    moment["end"],
                    sentence_spans,
                    clip_len,
                )
                # Topic-boundary guard: if the (sentence-snapped) end has bled
                # into the opening of a NEW topic, pull it back to where the
                # prior thought resolved. No-op when no segmentation ran.
                new_start, new_end = snap_end_off_next_topic(
                    new_start,
                    new_end,
                    topic_segments,
                    sentence_spans,
                    clip_len,
                )
                snapped.append({**moment, "start": new_start, "end": new_end})
            validated = snapped
            log.debug(
                "Sentence + topic boundary snapping applied",
                extra={
                    "span_count": len(sentence_spans),
                    "topic_count": len(topic_segments),
                    "moment_count": len(validated),
                },
            )
        else:
            log.debug("No sentence spans derived from transcript; skipping snap")
    except Exception as exc:  # pragma: no cover
        log.warning(
            "Sentence/topic boundary snapping failed; using raw LLM timestamps",
            extra={"error": str(exc)},
        )

    log.info(
        "LLM ranking complete",
        extra={"raw_count": len(moments_raw), "validated_count": len(validated)},
    )
    return validated
