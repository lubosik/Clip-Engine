"""
tests/test_topics.py — topic-boundary segmentation + snap guard.

The snap fixtures use the REAL timings from the peptide-start problem transcript
("Peptide Expert: What Do Peptides Actually Do?", youtube:jt5hHb6kzYM) — the run
where a clip bled into the opening of a new topic (the "app on your phone" answer)
instead of ending where the prior thought (the patient sperm-count story) resolved.
"""

from __future__ import annotations

import pytest

from core.topics import (
    FEWSHOT_BOUNDARY_EXAMPLES,
    _topic_index_at,
    _validate_topic_segments,
    segment_transcript,
    snap_end_off_next_topic,
)


# Real topic layout around the problem moment (seconds), abbreviated:
#   Topic A  1305 – 1370.4  : morbidly-obese patient / GLP-1 sperm-count story
#                             (resolves on "Started with a peptide.")
#   Topic B  1370.4 – 1420  : host asks for a high-level overview → "peptides are
#                             like an app on your phone" (a NEW topic)
PROBLEM_TOPICS = [
    {"start": 1305.0, "end": 1370.4, "summary": "GLP-1 patient story", "ends_because": "host asks new question"},
    {"start": 1371.0, "end": 1420.0, "summary": "app-on-your-phone overview", "ends_because": "subject change"},
]

# Sentence spans (end times) around the boundary — the resolving sentence ends at
# 1370.4; the new-topic opening sentence ends at ~1394.7.
PROBLEM_SPANS = [
    {"text": "But now we have peptides...", "start": 1343.9, "end": 1349.7},
    {"text": "And I just saw a patient...", "start": 1349.8, "end": 1366.0},
    {"text": "And that started with a peptide. Started with a peptide.", "start": 1366.2, "end": 1370.4},
    {"text": "So we've got lots of peptides on the table... overview?", "start": 1371.0, "end": 1391.0},
    {"text": "The best way to think about it is this. Peptides are almost like an app on your phone.", "start": 1391.8, "end": 1398.0},
]

CLIP_LEN = (20, 60)


class TestValidateTopicSegments:
    def test_drops_non_dicts_and_bad_times(self):
        raw = [
            {"start": 0.0, "end": 10.0, "summary": "a", "ends_because": "x"},
            "not a dict",
            {"start": 10.0, "end": 5.0},        # end <= start
            {"start": "bad", "end": 20.0},       # non-numeric
            {"start": 20.0, "end": 30.0},        # missing optional keys → OK
        ]
        out = _validate_topic_segments(raw)
        assert len(out) == 2
        assert out[0]["start"] == 0.0
        assert out[1]["summary"] == ""  # defaulted

    def test_sorted_by_start(self):
        raw = [
            {"start": 30.0, "end": 40.0},
            {"start": 0.0, "end": 10.0},
        ]
        out = _validate_topic_segments(raw)
        assert [s["start"] for s in out] == [0.0, 30.0]


class TestTopicIndexAt:
    def test_before_first(self):
        assert _topic_index_at(PROBLEM_TOPICS, 1000.0) == 0

    def test_inside_first(self):
        assert _topic_index_at(PROBLEM_TOPICS, 1350.0) == 0

    def test_inside_second(self):
        assert _topic_index_at(PROBLEM_TOPICS, 1395.0) == 1

    def test_after_last(self):
        assert _topic_index_at(PROBLEM_TOPICS, 9999.0) == 1

    def test_boundary_belongs_to_earlier_topic(self):
        # A resolving-word timestamp on the boundary stays in the topic it resolves.
        assert _topic_index_at(PROBLEM_TOPICS, 1370.4) == 0


class TestSnapEndOffNextTopic:
    def test_the_operator_case_end_bleeds_into_new_topic(self):
        # WRONG cut ended at 1398.0, inside the "app on your phone" new topic.
        start, end = snap_end_off_next_topic(
            1343.9, 1398.0, PROBLEM_TOPICS, PROBLEM_SPANS, CLIP_LEN
        )
        assert start == 1343.9
        # Trimmed back to where the prior thought resolved (≈1370.4), NOT 1398.0.
        assert end == pytest.approx(1370.4, abs=0.01)

    def test_end_already_within_start_topic_is_noop(self):
        start, end = snap_end_off_next_topic(
            1343.9, 1366.0, PROBLEM_TOPICS, PROBLEM_SPANS, CLIP_LEN
        )
        assert (start, end) == (1343.9, 1366.0)

    def test_no_topics_is_noop(self):
        assert snap_end_off_next_topic(10.0, 40.0, [], PROBLEM_SPANS, CLIP_LEN) == (10.0, 40.0)

    def test_single_topic_is_noop(self):
        one = [PROBLEM_TOPICS[0]]
        assert snap_end_off_next_topic(1343.9, 1398.0, one, PROBLEM_SPANS, CLIP_LEN) == (1343.9, 1398.0)

    def test_over_trim_below_min_returns_original(self):
        # If trimming to the topic boundary would make the clip too short, leave it.
        # start=1360 → boundary 1370.4 gives 10.4s < min 20 → original kept.
        start, end = snap_end_off_next_topic(
            1360.0, 1398.0, PROBLEM_TOPICS, PROBLEM_SPANS, CLIP_LEN
        )
        assert (start, end) == (1360.0, 1398.0)

    def test_snaps_to_sentence_end_when_no_exact_topic_span(self):
        # Topic boundary 1370.4 lands exactly on a sentence end here; the snapped
        # end must be a real sentence edge, never mid-sentence.
        _, end = snap_end_off_next_topic(
            1343.9, 1398.0, PROBLEM_TOPICS, PROBLEM_SPANS, CLIP_LEN
        )
        assert end in {s["end"] for s in PROBLEM_SPANS}


class TestFewshotExamples:
    def test_contains_real_sources_and_rule(self):
        # Drawn from real transcripts, not invented.
        assert "Peptide Expert" in FEWSHOT_BOUNDARY_EXAMPLES
        assert "app on your phone" in FEWSHOT_BOUNDARY_EXAMPLES
        assert "NEGATIVE" in FEWSHOT_BOUNDARY_EXAMPLES
        assert "NEVER end" in FEWSHOT_BOUNDARY_EXAMPLES


class TestSegmentTranscriptGraceful:
    def test_empty_transcript_returns_empty(self):
        assert segment_transcript([], CLIP_LEN) == []

    def test_missing_keys_returns_empty(self, monkeypatch):
        # With no LLM key configured, require_llm raises → caught → [].
        import core.settings

        def _boom():
            raise RuntimeError("LLM_API_KEY not set")

        # Force the settings path to raise; segment_transcript must swallow it.
        monkeypatch.setattr(
            core.settings, "get_settings", lambda: type("S", (), {"require_llm": staticmethod(_boom), "llm_base_url": None})()
        )
        assert segment_transcript([{"start": 0.0, "end": 5.0, "text": "hi there."}], CLIP_LEN) == []
