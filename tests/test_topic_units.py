"""
tests/test_topic_units.py — Tests for B1 (topic-unit segmentation) and B2
(clip_within_unit guard).

All tests are offline — zero LLM, zero network.

Coverage:
  B1:
    - segment_transcript returns completeness + boundary_reason fields
    - _validate_topic_segments handles completeness bool/string/absent
    - detect_unit_boundaries: question-ending, transition markers, openers, wrap-ups
    - build_units_from_boundaries: correct time ranges, single-unit fallback
    - TRANSITION_START_RE new patterns: 'another thing', 'so anyway',
      'and just like that'
  B2:
    - clip_within_unit: straddle → snapped
    - clip_within_unit: inside → unchanged
    - clip_within_unit: empty units → no-op
    - clip_within_unit: start before unit opening → moved up
    - clip_within_unit: multiple units
  B3:
    - REAL_BOUNDARY_PAIRS contains all 4 pair IDs
    - REAL_BOUNDARY_PAIRS is non-empty
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest

_PAIRS_FILE = (
    pathlib.Path(__file__).parent / "fixtures" / "segmentation" / "boundary_failure_pairs.json"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spans(*entries: tuple[str, float, float]) -> list[dict]:
    """Build sentence spans from (text, start, end) tuples."""
    return [{"text": t, "start": s, "end": e} for t, s, e in entries]


def _units(*entries: tuple[float, float]) -> list[dict]:
    """Build minimal unit dicts from (start, end) tuples."""
    return [
        {
            "start": s, "end": e,
            "summary": "", "boundary_reason": "test",
            "ends_because": "test", "completeness": True,
        }
        for s, e in entries
    ]


def _cand(start: float, end: float) -> dict:
    return {"start": start, "end": end, "score": 0.8, "hook": "", "reason": ""}


# ---------------------------------------------------------------------------
# B1 — _validate_topic_segments: completeness and boundary_reason
# ---------------------------------------------------------------------------

class TestValidateTopicSegments:
    def test_adds_completeness_true_when_bool(self):
        from core.topics import _validate_topic_segments
        raw = [{"start": 0.0, "end": 10.0, "summary": "s", "ends_because": "done", "completeness": True}]
        result = _validate_topic_segments(raw)
        assert len(result) == 1
        assert result[0]["completeness"] is True

    def test_adds_completeness_false_when_bool(self):
        from core.topics import _validate_topic_segments
        raw = [{"start": 0.0, "end": 10.0, "summary": "s", "ends_because": "mid", "completeness": False}]
        result = _validate_topic_segments(raw)
        assert result[0]["completeness"] is False

    def test_completeness_default_true_when_absent(self):
        from core.topics import _validate_topic_segments
        raw = [{"start": 0.0, "end": 10.0, "summary": "s", "ends_because": "done"}]
        result = _validate_topic_segments(raw)
        assert result[0]["completeness"] is True

    def test_completeness_from_string_false(self):
        from core.topics import _validate_topic_segments
        raw = [{"start": 0.0, "end": 10.0, "summary": "s", "ends_because": "done", "completeness": "false"}]
        result = _validate_topic_segments(raw)
        assert result[0]["completeness"] is False

    def test_boundary_reason_set_from_ends_because(self):
        from core.topics import _validate_topic_segments
        raw = [{"start": 0.0, "end": 10.0, "ends_because": "host asks new question"}]
        result = _validate_topic_segments(raw)
        assert result[0]["boundary_reason"] == "host asks new question"
        assert result[0]["ends_because"] == "host asks new question"

    def test_boundary_reason_preferred_over_ends_because(self):
        from core.topics import _validate_topic_segments
        raw = [{"start": 0.0, "end": 10.0, "ends_because": "old", "boundary_reason": "new value"}]
        result = _validate_topic_segments(raw)
        assert result[0]["boundary_reason"] == "new value"
        assert result[0]["ends_because"] == "old"  # back-compat preserved

    def test_backward_compat_ends_because_still_present(self):
        from core.topics import _validate_topic_segments
        raw = [{"start": 0.0, "end": 10.0, "ends_because": "wrap-up"}]
        result = _validate_topic_segments(raw)
        assert "ends_because" in result[0]
        assert "boundary_reason" in result[0]


# ---------------------------------------------------------------------------
# B1 — TRANSITION_START_RE new patterns (B1 additions)
# ---------------------------------------------------------------------------

class TestTransitionStartRENewPatterns:
    def test_another_thing_matches(self):
        from core.topics import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("Another thing to consider is BPC-157.")

    def test_another_thing_case_insensitive(self):
        from core.topics import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("ANOTHER THING about retatrutide...")

    def test_so_anyway_matches(self):
        from core.topics import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("So anyway, let's move on.")

    def test_so_anyway_case_insensitive(self):
        from core.topics import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("SO ANYWAY let's continue.")

    def test_and_just_like_that_matches(self):
        from core.topics import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("And just like that, the topic changed.")

    def test_and_just_like_still_matches(self):
        from core.topics import TRANSITION_START_RE
        # "and just like" (without "that") was in the original list
        assert TRANSITION_START_RE.match("And just like semaglutide works...")

    def test_new_patterns_available_via_boundary_check(self):
        """boundary_check.TRANSITION_START_RE is re-exported from core.topics."""
        from producer.boundary_check import TRANSITION_START_RE as BC_RE
        from core.topics import TRANSITION_START_RE as CT_RE
        assert BC_RE is CT_RE  # same object (re-import, not duplication)


# ---------------------------------------------------------------------------
# B1 — detect_unit_boundaries
# ---------------------------------------------------------------------------

class TestDetectUnitBoundaries:
    def test_empty_spans_returns_empty(self):
        from core.topics import detect_unit_boundaries
        assert detect_unit_boundaries([]) == []

    def test_single_span_returns_empty(self):
        from core.topics import detect_unit_boundaries
        spans = _spans(("Only one sentence.", 0.0, 5.0))
        assert detect_unit_boundaries(spans) == []

    def test_question_ending_does_not_create_boundary(self):
        """A mid-transcript '?' must NOT create a unit boundary — podcast
        speakers ask rhetorical/self-Q&A questions constantly, and splitting on
        every '?' shreds good clips (reviewer 2026-07-13). Starting a clip on an
        interviewer question is handled by is_bad_start_sentence instead."""
        from core.topics import detect_unit_boundaries
        spans = _spans(
            ("What is a peptide?", 0.0, 5.0),
            ("A peptide is a short chain of amino acids.", 5.0, 15.0),
        )
        boundaries = detect_unit_boundaries(spans)
        assert 1 not in boundaries

    def test_transition_marker_creates_boundary(self):
        from core.topics import detect_unit_boundaries
        spans = _spans(
            ("IGF-1 has a very short half-life.", 0.0, 10.0),
            ("Number 16, CAX is kind of like taking Adderall.", 10.0, 20.0),
            ("It lasts about 4 hours.", 20.0, 28.0),
        )
        boundaries = detect_unit_boundaries(spans)
        assert 1 in boundaries

    def test_topic_opener_creates_boundary(self):
        from core.topics import detect_unit_boundaries
        spans = _spans(
            ("And it's awesome.", 0.0, 5.0),
            ("So there's this interesting thing about peptides.", 5.0, 15.0),
        )
        boundaries = detect_unit_boundaries(spans)
        assert 1 in boundaries

    def test_wrap_up_cue_creates_boundary_after(self):
        from core.topics import detect_unit_boundaries
        spans = _spans(
            ("So that's why IGF-1 works better with this dosing.", 0.0, 10.0),
            ("The next topic is growth hormone.", 10.0, 20.0),
        )
        boundaries = detect_unit_boundaries(spans)
        assert 1 in boundaries

    def test_returns_sorted_list(self):
        from core.topics import detect_unit_boundaries
        spans = _spans(
            ("What is semaglutide?", 0.0, 5.0),    # next creates boundary
            ("It's a GLP-1 agonist.", 5.0, 10.0),   # starts new unit
            ("Now again, tirzepatide.", 10.0, 20.0), # transition → boundary
        )
        boundaries = detect_unit_boundaries(spans)
        assert boundaries == sorted(boundaries)

    def test_no_false_positives_on_clean_monologue(self):
        from core.topics import detect_unit_boundaries
        spans = _spans(
            ("Peptides are chains of amino acids.", 0.0, 5.0),
            ("They act on specific receptors.", 5.0, 10.0),
            ("For example, IGF-1 binds the insulin receptor.", 10.0, 18.0),
        )
        # None of these match any boundary pattern
        boundaries = detect_unit_boundaries(spans)
        assert len(boundaries) == 0


# ---------------------------------------------------------------------------
# B1 — build_units_from_boundaries
# ---------------------------------------------------------------------------

class TestBuildUnitsFromBoundaries:
    def test_no_boundaries_returns_single_unit(self):
        from core.topics import build_units_from_boundaries
        spans = _spans(
            ("Sentence one.", 0.0, 5.0),
            ("Sentence two.", 5.0, 12.0),
        )
        units = build_units_from_boundaries(spans, [])
        assert len(units) == 1
        assert units[0]["start"] == 0.0
        assert units[0]["end"] == 12.0

    def test_one_boundary_returns_two_units(self):
        from core.topics import build_units_from_boundaries
        spans = _spans(
            ("Topic A starts.", 0.0, 5.0),
            ("Topic B starts.", 5.0, 12.0),
            ("Topic B continues.", 12.0, 20.0),
        )
        units = build_units_from_boundaries(spans, [1])
        assert len(units) == 2
        assert units[0]["start"] == 0.0
        assert abs(units[0]["end"] - 5.0) < 0.01
        assert units[1]["start"] == 5.0
        assert abs(units[1]["end"] - 20.0) < 0.01

    def test_empty_spans_returns_empty(self):
        from core.topics import build_units_from_boundaries
        assert build_units_from_boundaries([], []) == []

    def test_out_of_range_boundaries_are_ignored(self):
        from core.topics import build_units_from_boundaries
        spans = _spans(("Sentence.", 0.0, 5.0))
        units = build_units_from_boundaries(spans, [99, -1, 0])
        # Only index 0 is filtered (boundary 0 is the first unit start, not included)
        # 99 is out of range, -1 is out of range
        assert len(units) == 1


# ---------------------------------------------------------------------------
# B2 — clip_within_unit
# ---------------------------------------------------------------------------

class TestClipWithinUnit:
    def test_empty_units_is_noop(self):
        from core.topics import clip_within_unit
        spans = _spans(("Sentence.", 0.0, 10.0))
        c = _cand(0.0, 10.0)
        result = clip_within_unit(c, [], spans)
        assert result is c  # unchanged object

    def test_empty_spans_is_noop(self):
        from core.topics import clip_within_unit
        units = _units((0.0, 10.0))
        c = _cand(0.0, 10.0)
        result = clip_within_unit(c, units, [])
        assert result is c

    def test_clip_inside_unit_unchanged(self):
        from core.topics import clip_within_unit
        units = _units((0.0, 30.0))
        spans = _spans(
            ("Sentence A.", 0.0, 10.0),
            ("Sentence B.", 10.0, 20.0),
            ("Sentence C.", 20.0, 30.0),
        )
        c = _cand(0.0, 20.0)
        result = clip_within_unit(c, units, spans)
        assert result["start"] == 0.0
        assert result["end"] == 20.0

    def test_end_bleeds_into_next_unit_is_snapped(self):
        """Clip end crosses into unit 2 → snapped to last sentence in unit 1."""
        from core.topics import clip_within_unit
        units = _units((0.0, 15.0), (15.0, 30.0))
        spans = _spans(
            ("Sentence A.", 0.0, 10.0),
            ("Sentence B.", 10.0, 15.0),
            ("Sentence C (unit 2).", 15.0, 25.0),
            ("Sentence D (unit 2).", 25.0, 30.0),
        )
        c = _cand(0.0, 20.0)  # end=20.0 is in unit 2 (15-30)
        result = clip_within_unit(c, units, spans)
        assert result["start"] == 0.0
        # End should be snapped back to end of last sentence in unit 1 (<=15.0)
        assert result["end"] <= 15.0 + 0.75  # within EPS of unit boundary

    def test_min_len_guard_prevents_over_trim(self):
        """clip_len min guard: an end-snap that would drop the clip below the
        minimum is skipped, keeping the LLM boundary (reviewer 2026-07-13)."""
        from core.topics import clip_within_unit
        units = _units((0.0, 12.0), (12.0, 45.0))
        spans = _spans(
            ("Unit 1 short sentence.", 0.0, 12.0),
            ("Unit 2 long content sentence.", 12.0, 45.0),
        )
        # Clip 0-45 straddles into unit 2; snapping to unit-1 end (12s) would
        # make an 12s clip, below the 18s minimum → must be left unchanged.
        c = _cand(0.0, 45.0)
        result = clip_within_unit(c, units, spans, clip_len=(18, 60))
        assert result["end"] == 45.0, "must NOT over-trim below clip_len min"

    def test_min_len_guard_allows_safe_snap(self):
        """When the snapped clip still meets the minimum, the snap applies."""
        from core.topics import clip_within_unit
        units = _units((0.0, 30.0), (30.0, 60.0))
        spans = _spans(
            ("Unit 1 content.", 0.0, 30.0),
            ("Unit 2 content.", 30.0, 60.0),
        )
        c = _cand(0.0, 55.0)  # snap to 30 → 30s clip >= 18 min, so snap applies
        result = clip_within_unit(c, units, spans, clip_len=(18, 60))
        assert result["end"] == 30.0

    def test_rhetorical_question_does_not_split_unit(self):
        """A mid-clip rhetorical '?' must NOT create a unit boundary (would
        shred good clips). Only transition/opener/wrap-up markers split."""
        from core.topics import detect_unit_boundaries
        spans = _spans(
            ("The best way is to titrate down slowly.", 0.0, 9.0),
            ("Why does that matter?", 9.0, 12.0),
            ("Because the body needs time to adjust.", 12.0, 21.0),
        )
        assert detect_unit_boundaries(spans) == []

    def test_start_before_unit_opening_moved_up(self):
        """Clip start is before unit's first sentence → moved to unit's sentence start."""
        from core.topics import clip_within_unit
        # Unit starts at 5.0 but clip start is at 3.0
        units = _units((5.0, 30.0))
        spans = _spans(
            ("Pre-unit sentence.", 0.0, 5.0),
            ("Unit opens here.", 5.0, 15.0),
            ("Unit continues.", 15.0, 25.0),
            ("Unit ends.", 25.0, 30.0),
        )
        c = _cand(3.0, 25.0)  # start=3.0 is before unit start=5.0
        result = clip_within_unit(c, units, spans)
        # start should move up to the sentence starting at or after 5.0
        assert result["start"] >= 5.0 - 0.01
        assert result["end"] == 25.0  # end unchanged

    def test_clip_across_three_units_snapped_to_first(self):
        """Clip spanning three units → end snapped to first unit's boundary."""
        from core.topics import clip_within_unit
        units = _units((0.0, 10.0), (10.0, 20.0), (20.0, 30.0))
        spans = _spans(
            ("Unit 1 sentence A.", 0.0, 5.0),
            ("Unit 1 sentence B.", 5.0, 10.0),
            ("Unit 2 sentence.", 10.0, 20.0),
            ("Unit 3 sentence.", 20.0, 30.0),
        )
        c = _cand(0.0, 25.0)  # spans units 1+2+3
        result = clip_within_unit(c, units, spans)
        # Should snap end back to within unit 1 (<=10.0)
        assert result["end"] <= 10.0 + 0.75

    def test_returns_new_dict_not_original(self):
        """clip_within_unit must return a new dict when adjusting."""
        from core.topics import clip_within_unit
        units = _units((0.0, 10.0), (10.0, 20.0))
        spans = _spans(
            ("Unit 1.", 0.0, 10.0),
            ("Unit 2.", 10.0, 20.0),
        )
        c = _cand(0.0, 15.0)
        result = clip_within_unit(c, units, spans)
        assert result is not c  # new dict
        assert c["end"] == 15.0  # original unmodified


# ---------------------------------------------------------------------------
# B3 — REAL_BOUNDARY_PAIRS contains all 4 pair IDs
# ---------------------------------------------------------------------------

class TestRealBoundaryPairs:
    def test_non_empty(self):
        from core.fewshot import REAL_BOUNDARY_PAIRS
        assert REAL_BOUNDARY_PAIRS.strip(), "REAL_BOUNDARY_PAIRS should not be empty"

    def test_all_four_pair_ids_present(self):
        from core.fewshot import REAL_BOUNDARY_PAIRS
        with open(_PAIRS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        for pair in data.get("pairs", []):
            assert pair["id"] in REAL_BOUNDARY_PAIRS, (
                f"Pair ID {pair['id']!r} not found in REAL_BOUNDARY_PAIRS"
            )

    def test_raw_pairs_loaded(self):
        from core.fewshot import REAL_BOUNDARY_PAIRS_RAW
        assert len(REAL_BOUNDARY_PAIRS_RAW) >= 1

    def test_wrong_and_correct_timestamps_in_text(self):
        from core.fewshot import REAL_BOUNDARY_PAIRS
        # Each pair should have its wrong start visible
        with open(_PAIRS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        for pair in data.get("pairs", []):
            wrong_start = str(pair["wrong"]["start"])
            assert wrong_start in REAL_BOUNDARY_PAIRS, (
                f"Wrong start {wrong_start} not in REAL_BOUNDARY_PAIRS for {pair['id']}"
            )

    def test_in_llm_prompt_sentence_index_mode(self):
        """REAL_BOUNDARY_PAIRS is injected into the ranking prompt (sentence-index mode)."""
        from core.fewshot import REAL_BOUNDARY_PAIRS
        # Build a minimal prompt and check the pairs appear
        # We mock the LLM call, so just check _build_prompt output
        from core.llm import _build_prompt
        spans = [{"text": "Test sentence.", "start": 0.0, "end": 5.0}]
        prompt = _build_prompt(
            transcript=[{"start": 0.0, "end": 5.0, "text": "Test."}],
            rules="Test rules.",
            comment_summary=None,
            clip_len=(15, 60),
            max_clips=3,
            sentence_spans=spans,
        )
        assert REAL_BOUNDARY_PAIRS[:50] in prompt or "REAL FAILURE CASES" in prompt

    def test_in_boundary_check_prompt(self):
        """REAL_BOUNDARY_PAIRS is injected into the boundary verification prompt."""
        from core.fewshot import REAL_BOUNDARY_PAIRS
        from producer.boundary_check import _build_boundary_prompt
        prompt = _build_boundary_prompt(
            before_sentences=["Before."],
            clip_sentences=["Clip sentence."],
            after_sentences=["After."],
        )
        assert "REAL FAILURE CASES" in prompt or REAL_BOUNDARY_PAIRS[:30] in prompt


# ---------------------------------------------------------------------------
# B1 — segment_transcript back-compat (LLM mocked)
# ---------------------------------------------------------------------------

class TestSegmentTranscriptBackCompat:
    def test_returns_list_of_dicts_with_new_fields(self):
        """segment_transcript output must include completeness + boundary_reason."""
        import os
        from unittest.mock import patch, MagicMock

        # Mock the Anthropic client
        mock_message = MagicMock()
        mock_message.content = [MagicMock(type="text", text=json.dumps([
            {
                "start": 0.0, "end": 15.0, "summary": "intro",
                "ends_because": "host question", "completeness": True,
            },
            {
                "start": 15.0, "end": 30.0, "summary": "explanation",
                "ends_because": "wrap-up", "completeness": False,
            },
        ]))]

        with patch("anthropic.Anthropic") as mock_anthropic, \
             patch.dict(os.environ, {"LLM_API_KEY": "test-key", "LLM_MODEL": "test-model"}):
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create.return_value = mock_message

            from core.topics import segment_transcript
            segments = [{"start": 0.0, "end": 30.0, "text": "Test transcript."}]
            result = segment_transcript(segments)

        assert len(result) == 2
        for topic in result:
            assert "completeness" in topic
            assert "boundary_reason" in topic
            assert "ends_because" in topic  # back-compat

    def test_returns_empty_on_missing_sdk(self):
        """segment_transcript must return [] when anthropic SDK is absent."""
        import importlib
        import sys
        with patch.dict(sys.modules, {"anthropic": None}):
            # Re-import to pick up the None mock
            from core import topics as t
            result = t.segment_transcript([{"start": 0.0, "end": 5.0, "text": "Test."}])
        assert result == []
