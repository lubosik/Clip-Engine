"""
tests/test_sentences.py — Unit tests for core/sentences.py

Covers:
  - _sentence_char_spans: boundary detection, abbreviation guard, ellipsis
  - build_sentence_spans: empty input, single sentence, multi-sentence, cross-
    segment sentences, timestamp monotonicity, "tissue repair after..." fixture
  - snap_to_sentences: start-snaps-down, end-snaps-forward, clip_len max trim,
    clip_len min extend, empty spans passthrough, out-of-range moments
"""

from __future__ import annotations

import math
import pytest

from core.sentences import (
    _sentence_char_spans,
    build_sentence_spans,
    snap_to_sentences,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Real transcript segments from the operator's video (provided in the task).
# Several segments END mid-sentence (e.g. "tissue repair after...").
FIXTURE_SEGMENTS = [
    {
        "start": 0.0,
        "end": 10.279,
        "text": (
            "Tessemaryllin, ipamaryllin, surmaryllin, MK677, these all stimulate"
            " the pituitary to release growth hormone, but they themselves are"
            " not growth hormone."
        ),
    },
    {
        "start": 10.49,
        "end": 19.17,
        "text": (
            "They will increase the amount of deep sleep that you get at night."
            " Typically you'll take these 30 minutes before sleep, ideally not"
            " having it eaten anything in the previous few hours."
        ),
    },
    {
        "start": 19.199,
        "end": 29.44,
        "text": (
            "Some of those things I just mentioned are FDA approved."
            " So the Sermorellin, for instance, to increase height, for instance,"
            " or tissue repair after..."
        ),
    },
]


def _sentence_ends(spans: list[dict]) -> list[float]:
    return [s["end"] for s in spans]


def _sentence_starts(spans: list[dict]) -> list[float]:
    return [s["start"] for s in spans]


# ---------------------------------------------------------------------------
# Tests: _sentence_char_spans  (low-level boundary detection)
# ---------------------------------------------------------------------------

class TestSentenceCharSpans:
    def test_empty_string(self):
        assert _sentence_char_spans("") == [(0, 0)]

    def test_single_sentence_no_boundary(self):
        text = "Hello world how are you"
        spans = _sentence_char_spans(text)
        assert spans == [(0, len(text))]

    def test_simple_period_split(self):
        text = "Hello world. How are you."
        spans = _sentence_char_spans(text)
        assert len(spans) == 2
        assert text[spans[0][0]:spans[0][1]].startswith("Hello")
        assert text[spans[1][0]:spans[1][1]].startswith("How")

    def test_exclamation_split(self):
        text = "Watch out! Something is happening."
        spans = _sentence_char_spans(text)
        assert len(spans) == 2
        assert text[spans[1][0]:spans[1][1]].startswith("Something")

    def test_question_split(self):
        text = "Is this working? Yes it is."
        spans = _sentence_char_spans(text)
        assert len(spans) == 2
        assert text[spans[1][0]:spans[1][1]].startswith("Yes")

    def test_ellipsis_not_split(self):
        """'...' must NOT trigger a sentence split, even when followed by a capital."""
        # All three dots in "..." are protected, so "... T" is not a boundary.
        text = "tissue repair after... They continued."
        spans = _sentence_char_spans(text)
        assert len(spans) == 1, (
            f"Expected 1 span (ellipsis protects all three dots) but got {len(spans)}"
        )
        assert spans[0] == (0, len(text))

    def test_ellipsis_in_middle_then_real_sentence(self):
        """'...' in the middle, then a proper period + capital = one real split."""
        # "...after... Normal" — the "..." is protected; "Normal" starts after ". N"
        # which is also inside the ellipsis (the last dot), so still protected.
        # But a REAL period after the ellipsis text should split.
        text = "tissue repair after... Normal sentence. Next starts here."
        spans = _sentence_char_spans(text)
        # The "." at end of "Normal sentence" followed by " N" is NOT in "..." so it splits.
        assert len(spans) == 2
        texts = [text[s:e].strip() for s, e in spans]
        assert any("tissue repair" in t for t in texts)
        assert any(t.startswith("Next") for t in texts)

    def test_dr_abbreviation_not_split(self):
        """'Dr.' must not trigger a sentence split."""
        text = "Dr. Smith said hello. Good morning."
        spans = _sentence_char_spans(text)
        # Only split at "hello. Good"
        assert len(spans) == 2
        assert text[spans[0][0]:spans[0][1]].startswith("Dr.")
        assert text[spans[1][0]:spans[1][1]].startswith("Good")

    def test_mr_abbreviation_not_split(self):
        text = "Mr. Jones arrived. He sat down."
        spans = _sentence_char_spans(text)
        assert len(spans) == 2
        assert text[spans[0][0]:spans[0][1]].startswith("Mr.")

    def test_multiple_boundaries(self):
        text = "First sentence. Second sentence. Third sentence."
        spans = _sentence_char_spans(text)
        assert len(spans) == 3

    def test_spans_cover_full_text(self):
        """Union of all spans must equal [0, len(text)]."""
        text = "One. Two! Three? Four."
        spans = _sentence_char_spans(text)
        assert spans[0][0] == 0
        assert spans[-1][1] == len(text)

    def test_consecutive_spans_are_contiguous(self):
        """Each span must start exactly where the previous one ended."""
        text = "First. Second. Third."
        spans = _sentence_char_spans(text)
        for i in range(1, len(spans)):
            assert spans[i][0] == spans[i - 1][1]

    def test_lowercase_after_period_not_split(self):
        """Period + space + lowercase must NOT split (mid-abbreviation or list)."""
        text = "These are e.g. examples. This is the next sentence."
        spans = _sentence_char_spans(text)
        # Only "examples. This" is a valid boundary (capital T follows)
        assert len(spans) == 2

    def test_single_capital_initial_not_split(self):
        """'A.' single capital initial must not trigger a split."""
        text = "A. Smith arrived. He sat down."
        spans = _sentence_char_spans(text)
        # "A." is protected; split only at "arrived. He"
        assert len(spans) == 2
        assert text[spans[1][0]:spans[1][1]].startswith("He")


# ---------------------------------------------------------------------------
# Tests: build_sentence_spans
# ---------------------------------------------------------------------------

class TestBuildSentenceSpans:
    def test_empty_transcript(self):
        assert build_sentence_spans([]) == []

    def test_empty_text_segments(self):
        segs = [{"start": 0.0, "end": 5.0, "text": "   "}]
        assert build_sentence_spans(segs) == []

    def test_single_segment_single_sentence(self):
        segs = [{"start": 0.0, "end": 10.0, "text": "Hello world how are you today."}]
        spans = build_sentence_spans(segs)
        assert len(spans) == 1
        assert spans[0]["text"].startswith("Hello")
        assert pytest.approx(spans[0]["start"], abs=0.01) == 0.0
        assert pytest.approx(spans[0]["end"], abs=0.01) == 10.0

    def test_single_segment_two_sentences(self):
        segs = [
            {
                "start": 0.0,
                "end": 20.0,
                "text": "First sentence is here. Second sentence follows now.",
            }
        ]
        spans = build_sentence_spans(segs)
        assert len(spans) == 2
        assert spans[0]["text"].startswith("First")
        assert spans[1]["text"].startswith("Second")

    def test_fixture_sentence_count(self):
        """Fixture should yield exactly 5 sentences from 3 segments."""
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        # Sentence breakdown:
        #   seg 0: 1 sentence (ends "...not growth hormone.")
        #   seg 1: 2 sentences (split at "at night. Typically")
        #   seg 2: 2 sentences (split at "FDA approved. So"; "after..." no split)
        assert len(spans) == 5

    def test_fixture_first_span_starts_at_seg0_start(self):
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        # First char of first segment → time 0.0
        assert pytest.approx(spans[0]["start"], abs=0.001) == 0.0

    def test_fixture_first_span_ends_at_seg0_end(self):
        """First sentence ends with 'not growth hormone.' — last char is in seg0."""
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        assert pytest.approx(spans[0]["end"], abs=0.1) == 10.279

    def test_fixture_second_span_starts_at_seg1_start(self):
        """'They will increase…' starts at the very first char of seg1 = 10.49."""
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        # Sentence 1 is "They will increase...at night." — first char of seg1
        assert pytest.approx(spans[1]["start"], abs=0.01) == 10.49

    def test_fixture_last_span_ends_at_seg2_end(self):
        """Last sentence ends at the end of seg2 = 29.44."""
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        assert pytest.approx(spans[-1]["end"], abs=0.1) == 29.44

    def test_fixture_ellipsis_not_split(self):
        """'tissue repair after...' must NOT be further split on '...'."""
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        last = spans[-1]["text"]
        assert "tissue repair after" in last
        # The entire last sentence from seg2 must be ONE span
        assert last.startswith("So the Sermorellin")

    def test_timestamps_monotonically_nondecreasing(self):
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        times = []
        for s in spans:
            times.append(s["start"])
            times.append(s["end"])
        for i in range(1, len(times)):
            assert times[i] >= times[i - 1] - 1e-9, (
                f"Time went backwards at index {i}: {times[i-1]} -> {times[i]}"
            )

    def test_sentence_end_before_or_at_next_sentence_start(self):
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        for i in range(1, len(spans)):
            assert spans[i]["start"] >= spans[i - 1]["end"] - 1e-9

    def test_timestamps_within_segment_bounds(self):
        """All span timestamps must be within the overall transcript time range."""
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        t_min = FIXTURE_SEGMENTS[0]["start"]
        t_max = FIXTURE_SEGMENTS[-1]["end"]
        for s in spans:
            assert s["start"] >= t_min - 1e-9
            assert s["end"] <= t_max + 1e-9

    def test_text_fields_are_non_empty(self):
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        for s in spans:
            assert s["text"].strip()

    def test_no_mid_sentence_timestamps(self):
        """
        The key property: for the LLM moment start=8.0, the snap should NOT
        yield 8.0 — it should land at a sentence start (0.0 is the only
        sentence start that is <= 8.0 AND contains 8.0 within [start, end]).
        """
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        # Sentence 0 spans 0.0 to ~10.279, so 8.0 is inside sentence 0.
        # Confirm sentence 0 contains 8.0.
        s0 = spans[0]
        assert s0["start"] <= 8.0 <= s0["end"]

    def test_segment_with_none_text_skipped(self):
        segs = [
            {"start": 0.0, "end": 5.0, "text": None},
            {"start": 5.0, "end": 10.0, "text": "Hello world today."},
        ]
        spans = build_sentence_spans(segs)
        assert len(spans) == 1
        assert spans[0]["text"].startswith("Hello")

    def test_cross_segment_sentence_timestamps(self):
        """
        A sentence that STARTS in one segment and would END in the next must
        have its start time from the first segment and end time from the second.
        For the fixture, seg0 contains sentence 0 entirely.  Sentence 1 starts
        at 10.49 (seg1 start) and ends within seg1.
        """
        spans = build_sentence_spans(FIXTURE_SEGMENTS)
        # Sentence 1 "They will...at night." starts at seg1.start (10.49)
        assert spans[1]["start"] >= FIXTURE_SEGMENTS[1]["start"] - 0.01
        assert spans[1]["end"] <= FIXTURE_SEGMENTS[1]["end"] + 0.01

    def test_single_character_segment(self):
        segs = [{"start": 0.0, "end": 1.0, "text": "A"}]
        spans = build_sentence_spans(segs)
        assert len(spans) == 1
        assert spans[0]["start"] == pytest.approx(0.0)
        assert spans[0]["end"] == pytest.approx(0.0)  # single char: t = start + 0 * dur = 0.0


# ---------------------------------------------------------------------------
# Tests: snap_to_sentences
# ---------------------------------------------------------------------------

class TestSnapToSentences:
    def _spans(self):
        return build_sentence_spans(FIXTURE_SEGMENTS)

    # --- empty / passthrough ---

    def test_empty_spans_returns_original(self):
        result = snap_to_sentences(8.0, 25.0, [], (15, 45))
        assert result == (8.0, 25.0)

    # --- start snapping ---

    def test_start_snaps_down_to_sentence_start(self):
        """
        moment_start=8.0 is inside sentence 0 (0.0 to ~10.279).
        Should snap DOWN to 0.0, not forward to 10.49.
        """
        spans = self._spans()
        new_start, _ = snap_to_sentences(8.0, 25.0, spans, (5, 60))
        assert pytest.approx(new_start, abs=0.01) == 0.0

    def test_start_is_not_raw_value(self):
        """Snapped start must differ from the raw 8.0."""
        spans = self._spans()
        new_start, _ = snap_to_sentences(8.0, 25.0, spans, (5, 60))
        assert abs(new_start - 8.0) > 0.1

    def test_start_before_all_spans_uses_span_zero(self):
        spans = self._spans()
        new_start, _ = snap_to_sentences(-5.0, 15.0, spans, (5, 60))
        assert pytest.approx(new_start, abs=0.01) == spans[0]["start"]

    def test_start_exactly_at_sentence_start(self):
        """If moment_start equals a sentence start, it stays there."""
        spans = self._spans()
        exact = spans[1]["start"]  # 10.49
        new_start, _ = snap_to_sentences(exact, exact + 10.0, spans, (5, 60))
        assert pytest.approx(new_start, abs=0.01) == exact

    def test_start_snaps_to_last_sentence_whose_start_is_lte(self):
        """
        moment_start=15.0 is after sentence 1's start (10.49) but before
        sentence 2's start (~13.7).  Wait — let me be precise: sentence 2
        "Typically..." starts around 13.7.  15.0 > 13.7 so it's after
        sentence 2 start, within sentence 2.  Snap should land on sentence 2
        start (~13.7), not sentence 1 start (10.49).
        """
        spans = self._spans()
        new_start, _ = snap_to_sentences(15.0, 25.0, spans, (5, 60))
        # new_start must be a sentence start (<= 15.0)
        starts = _sentence_starts(spans)
        assert any(abs(new_start - s) < 0.01 for s in starts)
        # And it must be the last such start that is <= 15.0
        valid_starts = [s for s in starts if s <= 15.0 + 1e-9]
        assert pytest.approx(new_start, abs=0.01) == max(valid_starts)

    # --- end snapping ---

    def test_end_is_not_raw_value(self):
        """Snapped end must differ from the raw mid-sentence 25.0."""
        spans = self._spans()
        _, new_end = snap_to_sentences(8.0, 25.0, spans, (5, 60))
        assert abs(new_end - 25.0) > 0.1

    def test_end_is_a_sentence_end(self):
        """Snapped end must coincide with one of the sentence ends."""
        spans = self._spans()
        _, new_end = snap_to_sentences(8.0, 25.0, spans, (5, 60))
        ends = _sentence_ends(spans)
        assert any(abs(new_end - e) < 0.01 for e in ends), (
            f"new_end={new_end} is not close to any sentence end: {ends}"
        )

    def test_end_after_all_spans_uses_last_span(self):
        spans = self._spans()
        _, new_end = snap_to_sentences(0.0, 999.0, spans, (5, 60))
        assert pytest.approx(new_end, abs=0.1) == spans[-1]["end"]

    def test_end_exactly_at_sentence_end(self):
        """If moment_end equals a sentence end, it stays there."""
        spans = self._spans()
        exact = spans[0]["end"]
        _, new_end = snap_to_sentences(0.0, exact, spans, (5, 60))
        assert pytest.approx(new_end, abs=0.01) == exact

    def test_mid_sentence_end_extends_to_full_sentence(self):
        """
        moment_end=25.0 is mid-sentence 4 ('So the Sermorellin...after...').
        Without clip_len trimming the end must snap to sentence 4's end (~29.44),
        NOT stop at 25.0.
        """
        spans = self._spans()
        _, new_end = snap_to_sentences(0.0, 25.0, spans, (5, 60))
        # 25.0 is inside sentence 4 (last sentence, end ~29.44)
        assert new_end > 25.0

    # --- the canonical operator scenario ---

    def test_operator_scenario_start_8_end_25(self):
        """
        The scenario from the task brief:
          LLM returns start=8.0, end=25.0.
          - start must snap to 0.0 (sentence 0 contains 8.0).
          - end must NOT be 25.0 (mid 'tissue repair after...').
          - end must land on a sentence end.
        """
        spans = self._spans()
        new_start, new_end = snap_to_sentences(8.0, 25.0, spans, (5, 60))

        assert pytest.approx(new_start, abs=0.01) == 0.0, (
            f"Expected start ~0.0 but got {new_start}"
        )
        assert abs(new_end - 25.0) > 0.1, (
            f"End must not be the raw 25.0 (mid-sentence), got {new_end}"
        )
        ends = _sentence_ends(spans)
        assert any(abs(new_end - e) < 0.01 for e in ends), (
            f"new_end={new_end} is not a sentence end; valid ends={ends}"
        )

    # --- clip_len max enforcement ---

    def test_clip_len_max_trims_trailing_sentences(self):
        """
        With max=15s and start snapped to 0.0, the snapped span covering all
        sentences (~29.44s) must be trimmed to <= 15s by dropping tail sentences.
        """
        spans = self._spans()
        new_start, new_end = snap_to_sentences(8.0, 25.0, spans, (5, 15))
        duration = new_end - new_start
        assert duration <= 15.0, f"Duration {duration:.2f} exceeds max 15s"

    def test_clip_len_max_trim_lands_on_sentence_end(self):
        spans = self._spans()
        new_start, new_end = snap_to_sentences(8.0, 25.0, spans, (5, 15))
        ends = _sentence_ends(spans)
        assert any(abs(new_end - e) < 0.01 for e in ends)

    def test_clip_len_max_trim_keeps_at_least_one_sentence(self):
        """
        Even with an impossibly small max, the first full sentence is preserved.
        """
        spans = self._spans()
        new_start, new_end = snap_to_sentences(8.0, 25.0, spans, (0, 1))
        # Should keep at least start sentence (sentence 0)
        assert new_end >= spans[0]["end"] - 0.01

    def test_clip_len_max_trim_operator_scenario_with_20s_max(self):
        """
        start=8.0→0.0, end=25.0→sentence containing 25.0 (≈29.44).
        Duration ≈29.44 > 20 → trim: drop last sentence → end ≈ sentence 3's end.
        Result duration must be <= 20.
        """
        spans = self._spans()
        new_start, new_end = snap_to_sentences(8.0, 25.0, spans, (5, 20))
        assert new_end - new_start <= 20.0
        ends = _sentence_ends(spans)
        assert any(abs(new_end - e) < 0.01 for e in ends)

    # --- clip_len min enforcement ---

    def test_clip_len_min_extends_by_adding_sentences(self):
        """
        moment_start=0.0, moment_end=5.0 → snaps to sentence 0 (≈10.279s).
        With min=20, must extend to include more sentences.
        """
        spans = self._spans()
        new_start, new_end = snap_to_sentences(0.0, 5.0, spans, (20, 45))
        assert new_end - new_start >= 20.0, (
            f"Duration {new_end - new_start:.2f}s is below min 20s"
        )

    def test_clip_len_min_extend_lands_on_sentence_end(self):
        spans = self._spans()
        new_start, new_end = snap_to_sentences(0.0, 5.0, spans, (20, 45))
        ends = _sentence_ends(spans)
        assert any(abs(new_end - e) < 0.01 for e in ends)

    def test_clip_len_min_when_not_enough_sentences_uses_all(self):
        """
        If min is larger than the entire transcript, snap uses all sentences
        and does not raise.
        """
        spans = self._spans()
        new_start, new_end = snap_to_sentences(0.0, 5.0, spans, (999, 9999))
        # Should still return the last sentence end (run out of sentences)
        assert pytest.approx(new_end, abs=0.1) == spans[-1]["end"]

    # --- ordering guard ---

    def test_start_idx_exceeds_end_idx_corrected(self):
        """
        Constructing a pathological case: moment_start after moment_end.
        start_idx might exceed end_idx; the guard should correct it.
        """
        spans = self._spans()
        # moment_start well into the last sentence; moment_end early
        last_start = spans[-1]["start"]
        new_start, new_end = snap_to_sentences(last_start + 1, 1.0, spans, (1, 60))
        # After ordering guard end_idx >= start_idx, so new_end >= new_start
        assert new_end >= new_start


# ---------------------------------------------------------------------------
# Tests: integration with rank_moments prompt (smoke test that import works)
# ---------------------------------------------------------------------------

class TestLlmIntegration:
    def test_sentences_imported_in_llm(self):
        """core.llm imports core.sentences without error."""
        import core.llm  # noqa: F401 — just verify it imports
        assert hasattr(core.llm, "rank_moments")

    def test_build_prompt_contains_sentence_boundary_instruction(self):
        from core.llm import _build_prompt
        prompt = _build_prompt(
            transcript=FIXTURE_SEGMENTS,
            rules="Test rules",
            comment_summary=None,
            clip_len=(20, 60),
            max_clips=3,
        )
        assert "FIRST word of a sentence" in prompt
        assert "LAST word of a sentence" in prompt
        assert "opening sentence" in prompt
