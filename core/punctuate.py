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
    - post-alignment text coverage is below _COVERAGE_THRESHOLD
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
    We use difflib.SequenceMatcher to build a GLOBAL word-level alignment
    between the model's output words and the original concatenated text words.
    This avoids the false-anchor problem of greedy sequential matching: a single
    mismatched token (censored word "[ __ ]", ">>" speaker marker, or
    punctuator divergence) in the old code caused orig_cursor to leap forward
    by thousands of words, silently dropping every subsequent sentence whose
    words fell behind the new cursor position.

Coverage guard:
    After alignment, coverage = (chars of aligned sentence text) / (chars of
    full_text) must be >= _COVERAGE_THRESHOLD (0.70).  Below that threshold
    restore_sentences logs a WARNING and returns None so the caller falls back
    to the regex sentence path, which uses all segment text.
"""

from __future__ import annotations

import difflib
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

# Minimum fraction of full_text chars that must be covered by aligned spans.
# Below this threshold restore_sentences returns None and the caller falls back
# to the regex sentence path.
_COVERAGE_THRESHOLD: float = 0.70


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
# Word-level helpers
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


# ---------------------------------------------------------------------------
# Global alignment via SequenceMatcher
# ---------------------------------------------------------------------------

def _align_sentences_to_times(
    sentences: list[str],
    full_text: str,
    char_times: list[float],
) -> list[dict] | None:
    """Map punctuation-restored sentences back to timestamps.

    Uses difflib.SequenceMatcher to build a global word-level alignment
    between the concatenated model output words and the original word list.
    This is robust to the false-anchor failure mode of the previous greedy
    scan: a single token mismatch (censored word, speaker marker, or
    punctuator divergence) can no longer cause orig_cursor to leap forward,
    silently dropping all subsequent sentences.

    Algorithm:
    1. Accumulate all model output words into a flat list, recording sentence
       boundaries (out_start, out_end) for each sentence.
    2. Run SequenceMatcher(output_words, orig_words) → monotonically increasing
       matching blocks.
    3. Build a sparse mapping out_word_idx → orig_word_idx from those blocks.
    4. For each sentence, find the first and last orig word indices mapped from
       its output word range.  Map those to char positions and timestamps.
    5. Post-process: drop any span whose orig_start_idx < the previous span's
       orig_end_idx (safety net; should be a no-op for well-ordered model output).

    Returns None when no sentences could be aligned.  Coverage checking is
    performed in restore_sentences, not here.
    """
    word_positions = _build_word_positions(full_text)
    if not word_positions:
        return None

    orig_words: list[str] = [w[2] for w in word_positions]

    # Accumulate model output words with sentence boundaries.
    output_words: list[str] = []
    sent_info: list[tuple[int, int, str]] = []  # (out_start, out_end_incl, text)

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        sent_words = [_normalize_word(w) for w in sentence.split() if _normalize_word(w)]
        if not sent_words:
            continue
        out_start = len(output_words)
        output_words.extend(sent_words)
        out_end = len(output_words) - 1
        sent_info.append((out_start, out_end, sentence))

    if not output_words or not sent_info:
        return None

    # Global word alignment.  autojunk=True (default) treats high-frequency
    # words as junk — this is intentional: common words like "the" / "and"
    # are exactly the false-anchor culprits in the old greedy code, and having
    # difflib skip them speeds up the match significantly (0.6 s vs 4.5 s on a
    # 22 K-word transcript).  The blocks returned are monotonically increasing
    # in both sequences, which guarantees temporal ordering of the final spans.
    matcher = difflib.SequenceMatcher(None, output_words, orig_words, autojunk=True)

    # Build sparse forward mapping: output_word_idx -> orig_word_idx.
    out_to_orig: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        a, b, size = block.a, block.b, block.size
        for k in range(size):
            out_to_orig[a + k] = b + k

    if not out_to_orig:
        log.warning("punctuate: SequenceMatcher found no matching words; returning None")
        return None

    # Map each sentence to its char-level and time-level span.
    result: list[dict] = []
    prev_orig_end: int = -1  # for the non-overlap safety filter (word index)

    for out_start, out_end, sentence_text in sent_info:
        orig_start_idx: int | None = None
        orig_end_idx: int | None = None

        for out_idx in range(out_start, out_end + 1):
            if out_idx in out_to_orig:
                oi = out_to_orig[out_idx]
                if orig_start_idx is None:
                    orig_start_idx = oi
                orig_end_idx = oi

        if orig_start_idx is None:
            # No matched words for this sentence — drop it.
            log.debug(
                "Punctuate: no aligned words for sentence %r; skipping",
                sentence_text[:60],
            )
            continue

        # Safety filter: drop spans that regress to already-covered orig positions.
        # In practice this is a no-op for well-ordered model output; it prevents
        # corruption if a sentence's junk-word-only boundary lands behind a
        # previous sentence's boundary.
        if orig_start_idx <= prev_orig_end:
            log.debug(
                "Punctuate: sentence %r orig_start %d <= prev_end %d; skipping",
                sentence_text[:40],
                orig_start_idx,
                prev_orig_end,
            )
            continue

        char_s = word_positions[orig_start_idx][0]
        char_e = word_positions[orig_end_idx][1] - 1
        char_e = max(char_s, char_e)  # guard against single-char words

        n = len(char_times)
        t_start = char_times[min(char_s, n - 1)]
        t_end = char_times[min(char_e, n - 1)]
        t_end = max(t_start, t_end)  # ensure non-negative duration

        result.append({
            "text": sentence_text,
            "start": t_start,
            "end": t_end,
        })
        prev_orig_end = orig_end_idx

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
        Returns None on any failure (model unavailable, alignment failure, or
        coverage below _COVERAGE_THRESHOLD) so callers can fall back to the
        regex path.
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

    # Coverage guard: if the aligned spans cover less than _COVERAGE_THRESHOLD
    # of the full text, the alignment has likely collapsed (false anchors,
    # chunk-boundary garbling, etc.).  Return None so the caller uses the regex
    # sentence path, which retains all segment text.
    aligned_chars = sum(len(s["text"]) for s in result)
    total_chars = len(full_text)
    coverage = aligned_chars / total_chars if total_chars > 0 else 0.0
    if coverage < _COVERAGE_THRESHOLD:
        log.warning(
            "punctuate: low coverage %.1f%% (%d/%d chars); "
            "falling back to regex sentence path",
            coverage * 100,
            aligned_chars,
            total_chars,
        )
        return None

    log.info(
        "Punctuation restoration complete: %d segments → %d sentences "
        "(coverage %.1f%%)",
        len(segments),
        len(result),
        coverage * 100,
    )
    return result
