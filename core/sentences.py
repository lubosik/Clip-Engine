"""
core/sentences.py — Sentence-boundary snapping for clip start/end timestamps.

Public interface:
    build_sentence_spans(transcript)         -> list[dict]
    snap_to_sentences(start, end, spans, clip_len) -> (float, float)

The module converts a segment-level transcript (each segment is often 1-3
sentences and frequently begins/ends mid-sentence) into sentence-level spans
with linearly-interpolated timestamps, then snaps LLM-chosen clip boundaries
so that every clip starts at the first word of a sentence and ends at the
last word of a sentence.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentence-splitting helpers
# ---------------------------------------------------------------------------

# Known abbreviations whose trailing "." must NOT trigger a sentence split.
# Matched case-insensitively.  Keep the list short — false negatives (extra
# splits) are harmless; false positives (missed splits) lose boundaries.
_ABBREV_RE = re.compile(
    r"\b(?:Dr|Mr|Mrs|Ms|Prof|Sr|Jr|vs|etc|approx|incl|no|vol|dept|est|corp|"
    r"ltd|co|ave|blvd|st|mt|ft|rev|fig|eq|approx)\.",
    re.IGNORECASE,
)

# Sentence boundary: one sentence-final punctuation char followed by
# one-or-more whitespace then a capital letter (lookahead — capital is NOT
# consumed, so m.end() lands exactly on the first char of the next sentence).
_SENT_BOUNDARY_RE = re.compile(r"([.!?])(\s+)(?=[A-Z])")


def _protected_dot_positions(text: str) -> set[int]:
    """
    Return character indices (of "." chars) that should NOT be treated as
    sentence-ending punctuation.

    Protected cases:
    - Ellipsis "..."  (all three dots protected)
    - Known abbreviations ("Dr.", "Mr.", etc.)
    - Single capital-letter initials ("A.", "I.")
    """
    protected: set[int] = set()

    # Ellipsis: protect all three dot positions.
    for m in re.finditer(r"\.\.\.", text):
        protected.update([m.start(), m.start() + 1, m.start() + 2])

    # Known abbreviations: protect the trailing dot.
    for m in _ABBREV_RE.finditer(text):
        protected.add(m.end() - 1)

    # Single capital-letter initials: "A.", "I.", "B.", …
    for m in re.finditer(r"\b([A-Z])\.", text):
        protected.add(m.end() - 1)

    return protected


def _sentence_char_spans(text: str) -> list[tuple[int, int]]:
    """
    Return ``(start, end)`` character-index pairs (end exclusive, Python
    slice convention) for each sentence in *text*.

    A sentence boundary is [.!?] followed by whitespace then a capital letter,
    unless the dot is in the protected set (abbreviation, ellipsis, initial).
    The entire text is always covered: the first span starts at 0 and the last
    span ends at len(text).
    """
    protected = _protected_dot_positions(text)

    # Positions in *text* where a new sentence starts.
    new_sentence_at: list[int] = [0]

    for m in _SENT_BOUNDARY_RE.finditer(text):
        dot_pos = m.start(1)
        if dot_pos in protected:
            continue
        # m.end() is the index of the first character of the next sentence
        # (the lookahead (?=[A-Z]) does not consume the capital letter).
        new_sentence_at.append(m.end())

    spans: list[tuple[int, int]] = []
    for i, start in enumerate(new_sentence_at):
        end = new_sentence_at[i + 1] if i + 1 < len(new_sentence_at) else len(text)
        spans.append((start, end))

    return spans


# ---------------------------------------------------------------------------
# Main public function 1 — build sentence spans with timestamps
# ---------------------------------------------------------------------------

def _build_char_time_map(
    transcript: list[dict],
) -> tuple[str, list[float]]:
    """
    Build a character-level timestamp map from a segment-level transcript.

    Returns ``(full_text, char_times)`` where ``char_times[i]`` is the
    interpolated timestamp for ``full_text[i]``.  Both lists have the same
    length.  Returns ``("", [])`` for an empty or whitespace-only transcript.

    Used by both :func:`build_sentence_spans` and
    :func:`core.punctuate.restore_sentences` to avoid duplicating the
    interpolation logic.
    """
    chars: list[str] = []
    char_times: list[float] = []
    prev_seg_end: float = 0.0

    for seg in transcript:
        seg_text: str = (seg.get("text") or "").strip()
        seg_start: float = float(seg["start"])
        seg_end: float = float(seg["end"])
        n: int = len(seg_text)

        if n == 0:
            prev_seg_end = seg_end
            continue

        if chars:
            chars.append(" ")
            char_times.append((prev_seg_end + seg_start) / 2.0)

        duration: float = seg_end - seg_start
        for j, ch in enumerate(seg_text):
            t = seg_start + (j / max(n - 1, 1)) * duration
            chars.append(ch)
            char_times.append(t)

        prev_seg_end = seg_end

    full_text: str = "".join(chars)
    if not full_text.strip():
        return "", []
    return full_text, char_times


def build_sentence_spans(transcript: list[dict]) -> list[dict]:
    """
    Convert a segment-level transcript into sentence-level time spans.

    Each input segment is ``{"start": float, "end": float, "text": str}``.
    Within each segment, character timestamps are assigned by linear
    interpolation between segment start and end.  Sentences that cross a
    segment boundary naturally get their start time from the first segment
    they appear in and their end time from the later segment.

    Args:
        transcript: list of segment dicts (start, end, text).

    Returns:
        list of ``{"text": str, "start": float, "end": float}`` in
        chronological order.  Returns an empty list for an empty or
        whitespace-only transcript.
    """
    if not transcript:
        return []

    # ------------------------------------------------------------------
    # Step 1: build parallel (character, timestamp) lists for the full
    #         concatenated text using the shared helper.
    # ------------------------------------------------------------------
    full_text, char_times = _build_char_time_map(transcript)
    if not full_text:
        return []

    # ------------------------------------------------------------------
    # Step 2: split the concatenated text into sentence character spans.
    # ------------------------------------------------------------------
    char_spans = _sentence_char_spans(full_text)

    # ------------------------------------------------------------------
    # Step 3: map character spans to time spans.
    # ------------------------------------------------------------------
    result: list[dict] = []
    for cs, ce in char_spans:
        raw: str = full_text[cs:ce]
        text: str = raw.rstrip()   # drop trailing whitespace / space separators
        if not text:
            continue

        t_start: float = char_times[cs]

        # Find the index of the last non-whitespace character in this span.
        # Using len(text) (after rstrip) avoids counting the stripped chars.
        last_char_idx: int = cs + len(text) - 1
        last_char_idx = min(last_char_idx, len(char_times) - 1)
        t_end: float = char_times[last_char_idx]

        result.append({"text": text, "start": t_start, "end": t_end})

    return result


# ---------------------------------------------------------------------------
# Main public function 2 — snap moment boundaries to sentence edges
# ---------------------------------------------------------------------------

def snap_to_sentences(
    moment_start: float,
    moment_end: float,
    sentence_spans: list[dict],
    clip_len: tuple[int, int],
) -> tuple[float, float]:
    """
    Snap *moment_start* to a sentence start and *moment_end* to a sentence end.

    Snapping rules:
    - **start**: find the sentence that *contains* moment_start
      (sentence.start <= moment_start) and snap DOWN to its beginning, so
      the opening words are never clipped.  If moment_start precedes all
      sentences, use sentence 0.
    - **end**: find the first sentence whose end >= moment_end (the sentence
      that contains, or just follows, the raw end time) and snap to its END,
      so the final thought is never cut off mid-word.  If moment_end is after
      all sentences, use the last sentence.

    Clip-length enforcement (applied after snapping, using whole sentences only):
    - If duration > *clip_len[1]* (max): drop trailing sentences one at a time
      until duration <= max.  At minimum, one sentence (start_idx sentence) is
      always kept.
    - If duration < *clip_len[0]* (min): append following sentences one at a
      time until duration >= min or no more sentences remain.

    Args:
        moment_start:    raw LLM-chosen start time (seconds).
        moment_end:      raw LLM-chosen end time (seconds).
        sentence_spans:  output of build_sentence_spans().
        clip_len:        (min_seconds, max_seconds).

    Returns:
        (new_start, new_end) — both on sentence boundaries.
        Returns the original (moment_start, moment_end) unchanged if
        sentence_spans is empty.
    """
    if not sentence_spans:
        return moment_start, moment_end

    min_len, max_len = clip_len

    # ------------------------------------------------------------------
    # Find start sentence index: last sentence whose start <= moment_start
    # (snap DOWN — prefer the sentence the moment starts within).
    # ------------------------------------------------------------------
    start_idx: int = 0
    for i, span in enumerate(sentence_spans):
        if span["start"] <= moment_start:
            start_idx = i
        # No early exit: keep walking to find the LAST qualifying span.

    # ------------------------------------------------------------------
    # Find end sentence index: first sentence whose end >= moment_end
    # (extend to include the full sentence, never cut mid-thought).
    # ------------------------------------------------------------------
    end_idx: int = len(sentence_spans) - 1  # default: last sentence
    for i, span in enumerate(sentence_spans):
        if span["end"] >= moment_end:
            end_idx = i
            break

    # Ensure valid ordering (can happen if moment spans no full sentences).
    if end_idx < start_idx:
        end_idx = start_idx

    new_start: float = sentence_spans[start_idx]["start"]
    new_end: float = sentence_spans[end_idx]["end"]

    # ------------------------------------------------------------------
    # Enforce max duration: drop trailing whole sentences.
    # Always keep at least the sentence that contains moment_start.
    # ------------------------------------------------------------------
    while (new_end - new_start) > max_len and end_idx > start_idx:
        end_idx -= 1
        new_end = sentence_spans[end_idx]["end"]

    # ------------------------------------------------------------------
    # Enforce min duration: add following whole sentences.
    # ------------------------------------------------------------------
    while (new_end - new_start) < min_len and end_idx < len(sentence_spans) - 1:
        end_idx += 1
        new_end = sentence_spans[end_idx]["end"]

    return new_start, new_end
