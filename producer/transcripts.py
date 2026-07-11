"""
producer/transcripts.py — transcript fetch, normalisation, and persistence.

Per SPEC §4 stage 4: fetch transcripts for selected sources via Apify actors,
normalize to [{start, end, text}], and persist to the transcripts table.

Once a transcript row exists for a source_id, it is never re-fetched.

Actor IDs per SPEC §3:
  YouTube:   pintostudio/youtube-transcript-scraper  — [{start, dur, text}]
  TikTok:    agentx/tiktok-transcript                — transcript.segments [{start, end, text}]
  Instagram: transcript embedded in instagram-reel-scraper item

Normalised segment shape: {start: float, end: float, text: str}
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.apify import Apify
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

ACTOR_YT_TRANSCRIPT = "pintostudio/youtube-transcript-scraper"
ACTOR_TT_TRANSCRIPT = "agentx/tiktok-transcript"


class TranscriptFetchError(RuntimeError):
    """The transcript actor RUN failed (outage, usage limit, auth).

    Distinct from a video that genuinely has no transcript (which returns []).
    Callers must NOT mark the source done on this error — the fetch should be
    retried on a future run.
    """


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _norm_yt_segments(raw_items: list[dict]) -> list[dict]:
    """
    pintostudio/youtube-transcript-scraper returns [{start, dur, text}].
    Normalise to [{start, end, text}].
    """
    segments = []
    for item in raw_items:
        try:
            start = float(item["start"])
            dur = float(item["dur"])
            text = str(item.get("text") or "").strip()
            if text:
                segments.append({"start": start, "end": start + dur, "text": text})
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed YT segment", extra={"error": str(exc), "item": item})
    return segments


def _norm_tiktok_segments(raw_transcript: dict) -> list[dict]:
    """
    agentx/tiktok-transcript returns a dict with a "transcript" key that has
    a "segments" list of {start, end, text}.
    """
    segments_raw = []

    # Handle both possible structures
    if isinstance(raw_transcript, dict):
        inner = raw_transcript.get("transcript") or raw_transcript
        if isinstance(inner, dict):
            segments_raw = inner.get("segments") or []
        elif isinstance(inner, list):
            segments_raw = inner

    result = []
    for seg in segments_raw:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg["start"])
            end = float(seg["end"])
            text = str(seg.get("text") or "").strip()
            if text:
                result.append({"start": start, "end": end, "text": text})
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed TT segment", extra={"error": str(exc), "seg": seg})
    return result


def _norm_ig_transcript(raw_item: dict) -> list[dict]:
    """
    Extract transcript from an instagram-reel-scraper item.
    The transcript field, when present, is typically a string or a list of segments.
    """
    transcript_field = raw_item.get("transcript") or raw_item.get("captionText")
    if not transcript_field:
        return []

    # If it's a list of segment dicts
    if isinstance(transcript_field, list):
        result = []
        for seg in transcript_field:
            if isinstance(seg, dict):
                try:
                    start = float(seg.get("start") or seg.get("startTime") or 0)
                    end = float(seg.get("end") or seg.get("endTime") or 0)
                    text = str(seg.get("text") or "").strip()
                    if text and end > start:
                        result.append({"start": start, "end": end, "text": text})
                except (TypeError, ValueError):
                    pass
        return result

    # If it's a plain string, return as a single segment (no timing)
    if isinstance(transcript_field, str) and transcript_field.strip():
        log.debug("Instagram transcript is plain text; no timestamps available")
        return [{"start": 0.0, "end": 0.0, "text": transcript_field.strip()}]

    return []


# ---------------------------------------------------------------------------
# Fetch functions (call Apify)
# ---------------------------------------------------------------------------

def fetch_youtube_transcript(url: str, apify: "Apify") -> list[dict]:
    """Fetch and normalise YouTube transcript segments."""
    # pintostudio/youtube-transcript-scraper requires a single `videoUrl` string;
    # it rejects the `startUrls` list shape ("Field input.videoUrl is required").
    run_input: dict[str, Any] = {"videoUrl": url}
    try:
        items = apify.run(ACTOR_YT_TRANSCRIPT, run_input)
    except Exception as exc:
        log.error(
            "YouTube transcript fetch failed",
            extra={"url": url, "error": str(exc)},
        )
        raise TranscriptFetchError(f"YouTube transcript actor failed: {exc}") from exc

    if not items:
        log.warning("No transcript returned for YouTube video", extra={"url": url})
        return []

    # The actor returns one item per video with the segment list under `data`:
    #   {"data": [{"start": "0.52", "dur": "3.72", "text": "..."}, ...]}
    # (start/dur are strings; _norm_yt_segments coerces them). Older/alternate
    # shapes used transcript/captions/subtitles — keep those as fallbacks.
    raw_item = items[0]
    segments_raw = (
        raw_item.get("data")
        or raw_item.get("transcript")
        or raw_item.get("captions")
        or raw_item.get("subtitles")
        or []
    )
    if isinstance(segments_raw, list):
        # Detect shape: might already be normalised or in {start,dur,text} form
        if segments_raw and isinstance(segments_raw[0], dict):
            if "dur" in segments_raw[0]:
                return _norm_yt_segments(segments_raw)
            elif "end" in segments_raw[0]:
                # Already {start, end, text}
                return [
                    {"start": float(s.get("start", 0)), "end": float(s.get("end", 0)), "text": str(s.get("text", "")).strip()}
                    for s in segments_raw if s.get("text")
                ]

    log.warning("Unexpected YouTube transcript item shape", extra={"url": url, "keys": list(raw_item)})
    return []


def fetch_tiktok_transcript(url: str, apify: "Apify") -> list[dict]:
    """Fetch and normalise TikTok transcript segments (costs ~$0.38/video)."""
    run_input: dict[str, Any] = {"postURLs": [url]}
    try:
        items = apify.run(ACTOR_TT_TRANSCRIPT, run_input)
    except Exception as exc:
        log.error(
            "TikTok transcript fetch failed",
            extra={"url": url, "error": str(exc)},
        )
        raise TranscriptFetchError(f"TikTok transcript actor failed: {exc}") from exc

    if not items:
        log.warning("No transcript returned for TikTok video", extra={"url": url})
        return []

    return _norm_tiktok_segments(items[0])


def extract_instagram_transcript(raw_item: dict) -> list[dict]:
    """Extract transcript from an already-fetched instagram-reel-scraper item."""
    return _norm_ig_transcript(raw_item)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def transcript_exists(session: "Session", source_id: str) -> bool:
    """Return True if a transcript row already exists for this source_id."""
    from core.models import Transcript
    return session.query(Transcript.id).filter_by(source_id=source_id).first() is not None


def store_transcript(
    session: "Session",
    source_id: str,
    segments: list[dict],
    *,
    word_level: bool = False,
) -> None:
    """
    Persist transcript segments to the transcripts table.
    Idempotent: does nothing if a row already exists.
    """
    from core.models import Transcript

    if transcript_exists(session, source_id):
        log.debug("Transcript already stored; skipping", extra={"source_id": source_id})
        return

    row = Transcript(
        source_id=source_id,
        segments=segments,
        word_level=word_level,
    )
    session.add(row)
    log.info(
        "Stored transcript",
        extra={
            "source_id": source_id,
            "segments": len(segments),
            "word_level": word_level,
        },
    )


def get_transcript(session: "Session", source_id: str) -> list[dict] | None:
    """
    Return stored transcript segments for source_id, or None if not found.
    """
    from core.models import Transcript
    row = session.query(Transcript).filter_by(source_id=source_id).first()
    if row is None:
        return None
    return row.segments or []


def fetch_and_store_transcript(
    session: "Session",
    source_id: str,
    platform: str,
    url: str,
    apify: "Apify",
    *,
    ig_raw_item: dict | None = None,
) -> list[dict]:
    """
    Fetch transcript if not already stored, persist, and return segments.

    For Instagram, pass ig_raw_item (the discovery item) to avoid a second actor call.
    """
    existing = get_transcript(session, source_id)
    if existing is not None:
        log.info(
            "Using cached transcript",
            extra={"source_id": source_id, "segments": len(existing)},
        )
        return existing

    if platform == "youtube":
        segments = fetch_youtube_transcript(url, apify)
    elif platform == "tiktok":
        segments = fetch_tiktok_transcript(url, apify)
    elif platform == "instagram":
        if ig_raw_item:
            segments = extract_instagram_transcript(ig_raw_item)
        else:
            log.warning(
                "No ig_raw_item provided for Instagram transcript; cannot fetch",
                extra={"source_id": source_id},
            )
            segments = []
    else:
        log.warning(
            "Unknown platform for transcript fetch",
            extra={"platform": platform, "source_id": source_id},
        )
        segments = []

    if segments:
        store_transcript(session, source_id, segments)
        session.flush()
    else:
        log.warning(
            "No transcript segments obtained",
            extra={"source_id": source_id, "platform": platform},
        )

    return segments
