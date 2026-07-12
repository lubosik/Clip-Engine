"""
core/punctuate.py — Punctuation restoration pre-pass for transcript sentence splitting.

Public interface:
    restore_sentences(segments: list[dict]) -> list[dict] | None

Takes raw transcript segments (the same [{start, end, text}] list used
everywhere else), concatenates their text, runs a punctuation-restoration
model, splits the restored text into sentences, and maps each sentence back
to timestamps via the char→time interpolation shared with core/sentences.py.

Returns:
    list[{"text": str, "start": float, "end": float}]  — punctuated sentence
    spans suitable for replacing the output of build_sentence_spans().
    Returns None when:
    - the model is unavailable (import error, download failure, runtime error)
    - the transcript is empty
    The caller must fall back to the existing regex path in that case.

Model:
    PunctCapSegModelONNX("pcs_en") from the `punctuators` package.
    Uses ONNX Runtime (CPU), no CUDA required.  The package also pulls in
    torch as a dependency but the inference path is ONNX-only.
    Model is loaded ONCE per process and cached at module level.

Chunking:
    The model's infer() method already handles long texts internally via
    batch_size_tokens=4096 with overlap=16, so we do not need manual chunking
    beyond what the library provides.

Alignment strategy:
    The model preserves word ORDER and only adds punctuation / capitalisation.
    We do a sequential word-level alignment: for each output sentence, we
    find its constituent words (stripped of added punctuation) in the original
    concatenated text and record their char positions.  Those positions are
    then mapped to timestamps via the char_times array built by
    core.sentences._build_char_time_map.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.sentences import _build_char_time_map

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level model cache (loaded once per process, lazily)
# ---------------------------------------------------------------------------

_model: Any = None          # PunctCapSegModelONNX instance once loaded
_model_load_failed: bool = False  # True after a permanent load failure


def _get_model() -> Any | None:
    """Load (or return cached) the PunctCapSegModelONNX model.

    Returns None and sets _model_load_failed on any error so we never
    retry a permanently broken environment.
    """
    global _model, _model_load_failed
    if _model is not None:
        return _model
    if _model_load_failed:
        return None

    try:
        from punctuators.models import PunctCapSegModelONNX  # type: ignore[import]
        _model = PunctCapSegModelONNX.from_pretrained("pcs_en")
        log.info("punctuators PunctCapSegModelONNX(pcs_en) loaded")
        return _model
    except Exception as exc:
        _model_load_failed = True
        log.warning(
            "punctuators model load failed; punctuation restoration disabled: %s",
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Word-level sequential alignment
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\S+")
_NONWORD_CHARS_RE = re.compile(r"[^\w'\-]", re.UNICODE)


def _normalize_word(w: str) -> str:
    """Strip punctuation and lowercase for comparison."""
    return _NONWORD_CHARS_RE.sub("", w).lower()


def _build_word_positions(text: str) -> list[tuple[int, int, str]]:
    """Return [(char_start, char_end, normalized_word), ...] for every token."""
    return [
        (m.start(), m.end(), _normalize_word(m.group()))
        for m in _WORD_RE.finditer(text)
    ]


def _align_sentences_to_times(
    sentences: list[str],
    full_text: str,
    char_times: list[float],
) -> list[dict] | None:
    """Map punctuation-restored sentences back to timestamps.

    For each output sentence, the constituent words (stripped of added
    punctuation) are matched sequentially against the original word list.
    The char positions of the first and last matched words are mapped to
    timestamps via char_times.

    Returns None when alignment yields no usable spans (e.g. model output
    completely diverged from the original text).
    """
    word_positions = _build_word_positions(full_text)
    if not word_positions:
        return None

    result: list[dict] = []
    orig_cursor = 0  # index into word_positions

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # Words in this output sentence (normalized for matching)
        sent_tokens = sentence.split()
        sent_words = [_normalize_word(w) for w in sent_tokens if _normalize_word(w)]
        if not sent_words:
            continue

        # Find the span of original words that corresponds to this sentence.
        # We search forward from orig_cursor.
        found_start: int | None = None
        found_end: int | None = None
        word_match_cursor = 0  # how many sent_words we have matched so far
        temp_orig_cursor = orig_cursor

        for i in range(orig_cursor, len(word_positions)):
            orig_word_norm = word_positions[i][2]
            if orig_word_norm == sent_words[word_match_cursor]:
                if word_match_cursor == 0:
                    found_start = word_positions[i][0]   # char start of first word
                word_match_cursor += 1
                if word_match_cursor >= len(sent_words):
                    found_end = word_positions[i][1] - 1  # char end of last word
                    temp_orig_cursor = i + 1
                    break
            else:
                # Mismatch — re-anchor to this position if it matches first sent word
                if orig_word_norm == sent_words[0]:
                    found_start = word_positions[i][0]
                    word_match_cursor = 1
                    if word_match_cursor >= len(sent_words):
                        found_end = word_positions[i][1] - 1
                        temp_orig_cursor = i + 1
                        break
                else:
                    if word_match_cursor > 0:
                        # Reset match attempt
                        word_match_cursor = 0
                        found_start = None

        if found_start is None or found_end is None:
            # Alignment failed for this sentence; skip but don't abort everything
            log.debug(
                "Punctuate: could not align sentence %r to original text; skipping",
                sentence[:60],
            )
            continue

        orig_cursor = temp_orig_cursor

        t_start = char_times[min(found_start, len(char_times) - 1)]
        t_end = char_times[min(found_end, len(char_times) - 1)]

        result.append({
            "text": sentence,
            "start": t_start,
            "end": t_end,
        })

    return result if result else None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def restore_sentences(segments: list[dict]) -> list[dict] | None:
    """Restore punctuation and return sentence-level time spans.

    Args:
        segments:  Raw transcript segments [{start, end, text}].

    Returns:
        list[{"text": str, "start": float, "end": float}] — one entry per
        sentence, with timestamps derived from character-level interpolation.
        Returns None on any failure so callers can fall back to the regex path.
    """
    if not segments:
        return None

    # Build char→time map (shared with build_sentence_spans)
    full_text, char_times = _build_char_time_map(segments)
    if not full_text or not char_times:
        return None

    # Load the model (lazy + cached)
    model = _get_model()
    if model is None:
        return None

    try:
        # The model handles chunking internally (batch_size_tokens=4096, overlap=16)
        raw_results: list[list[str]] = model.infer([full_text])
        if not raw_results or not raw_results[0]:
            log.warning("punctuators returned empty result")
            return None
        sentences: list[str] = raw_results[0]
    except Exception as exc:
        log.warning("punctuators inference failed: %s", exc)
        return None

    # Align output sentences back to timestamps
    try:
        result = _align_sentences_to_times(sentences, full_text, char_times)
    except Exception as exc:
        log.warning("punctuate alignment failed: %s", exc)
        return None

    if not result:
        log.warning("punctuate: alignment produced no spans; returning None")
        return None

    log.info(
        "Punctuation restoration complete: %d segments → %d sentences",
        len(segments),
        len(result),
    )
    return result
