"""
tests/test_transition_trim.py --- Tests for TRANSITION_START_RE and
trim_trailing_transition (Req B1) in producer/boundary_check.py.

No network calls, no LLM mocking needed (pure functions).

Covers:
  TRANSITION_START_RE:
    - Matches: "Number N+" (digits), "Number [A-Z]", "Next up", "The next one",
               "Now again", "And just like", "Also,", "Oh, and",
               "So the next", "Moving on", "Alright next", "Alright, next"
    - Case-insensitive
    - Does NOT match clean ending sentences

  trim_trailing_transition:
    - clip-80 case: "Number 16, CAX" last sentence -> trim to prior sentence
    - No-op when trim would violate clip_len minimum
    - Various transition markers each trigger a trim
    - No marker -> unchanged (returns original dict)
    - Empty spans -> unchanged
    - Last sentence at index 0 -> no-op (cannot trim further)

  apply_prefilters integration:
    - clip-80 case: apply_prefilters trims the transition sentence from the end
    - Normal prefilter behaviour unchanged when no transition sentence present
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spans(*texts_with_times: tuple[str, float, float]) -> list[dict]:
    return [{"text": t, "start": s, "end": e} for t, s, e in texts_with_times]


def _candidate(start: float, end: float, score: float = 0.8) -> dict:
    return {"start": start, "end": end, "score": score, "hook": "Test hook", "reason": ""}


# ---------------------------------------------------------------------------
# TRANSITION_START_RE — pattern matching
# ---------------------------------------------------------------------------

class TestTransitionStartRe:

    def test_number_digit_matches(self):
        """'Number 16' matches the transition pattern."""
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("Number 16, CAX. This is like Adderall...")

    def test_number_letter_matches(self):
        """'Number A' matches (lettered list item)."""
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("Number A, first point")

    def test_next_up_matches(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("Next up, we have semaglutide.")

    def test_the_next_one_matches(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("The next one is BPC-157.")

    def test_now_again_matches(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("Now again, retatrutide dosing...")

    def test_and_just_like_matches(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("And just like semaglutide...")

    def test_also_matches(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("Also, there's another consideration.")

    def test_oh_and_matches(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("Oh, and another thing...")

    def test_so_the_next_matches(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("So the next peptide on the list is...")

    def test_moving_on_matches(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("Moving on to the next topic...")

    def test_alright_next_matches(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("Alright next we have tirzepatide.")

    def test_alright_comma_next_matches(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("Alright, next on our list...")

    def test_case_insensitive_number(self):
        """Lowercase 'number 16' also matches (case-insensitive)."""
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("number 16, something else")

    def test_case_insensitive_moving_on(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert TRANSITION_START_RE.match("MOVING ON to the next thing.")

    def test_clean_statement_no_match(self):
        """A normal sentence does not match."""
        from producer.boundary_check import TRANSITION_START_RE
        assert not TRANSITION_START_RE.match("BPC-157 accelerates tendon healing.")

    def test_mid_sentence_number_no_match(self):
        """'Number' appearing mid-sentence (not at start) does not match."""
        from producer.boundary_check import TRANSITION_START_RE
        # The regex is anchored at ^ so mid-sentence occurrences are ignored
        assert not TRANSITION_START_RE.match("The Number 16 peptide is interesting.")

    def test_selank_sentence_no_match(self):
        """The Selank ending sentence does not match — it's a clean ending."""
        from producer.boundary_check import TRANSITION_START_RE
        assert not TRANSITION_START_RE.match(
            "I'm not sure that I would try this one again."
        )

    def test_empty_string_no_match(self):
        from producer.boundary_check import TRANSITION_START_RE
        assert not TRANSITION_START_RE.match("")


# ---------------------------------------------------------------------------
# trim_trailing_transition — pure function
# ---------------------------------------------------------------------------

class TestTrimTrailingTransition:

    def test_clip80_case_trims_list_item(self):
        """clip-80: 'Number 16, CAX' is the last sentence → trim to prior sentence.

        Selank idea resolves at ~238.4s ("...try this one again.").
        'Number 16, CAX' starts at 238.4 and bleeds to ~240.
        After trim, clip end should be 238.4 (end of the Selank sentence).
        """
        from producer.boundary_check import trim_trailing_transition

        spans = _spans(
            ("...some people report having a lot less daily anxiety...", 220.0, 232.7),
            ("Some people have worse anxiety.", 232.7, 236.9),
            ("I'm not sure that I would try this one again.", 236.9, 238.4),
            ("Number 16, CAX. This is kind of like taking Adderall...", 238.4, 242.0),
        )
        # Candidate end = 238.9 (inside the 'Number 16' sentence which ends at 242)
        c = _candidate(start=220.0, end=238.9)
        result = trim_trailing_transition(c, spans, clip_len=(10, 120))

        assert result["end"] == 238.4, (
            f"Expected end=238.4 (after Selank sentence), got {result['end']}"
        )
        assert result["start"] == 220.0  # start unchanged

    def test_noop_when_min_len_would_break(self):
        """No trim when the resulting clip would be shorter than clip_len[0]."""
        from producer.boundary_check import trim_trailing_transition

        spans = _spans(
            ("The peptide discussion resolves here.", 0.0, 5.0),
            ("Number 16, CAX. Next peptide.", 5.0, 10.0),
        )
        # start=0, end=10, after trim would be 5 → duration=5 which equals min_len=6 → no-op
        c = _candidate(start=0.0, end=10.0)
        result = trim_trailing_transition(c, spans, clip_len=(6, 120))

        # Would produce 5s clip (0→5), but min_len=6, so no-op
        assert result["end"] == 10.0

    def test_trim_allowed_when_min_len_met(self):
        """Trim IS applied when resulting duration >= clip_len[0]."""
        from producer.boundary_check import trim_trailing_transition

        spans = _spans(
            ("BPC-157 accelerates healing.", 0.0, 5.0),
            ("Studies confirm 80% improvement.", 5.0, 15.0),
            ("Moving on to the next peptide.", 15.0, 20.0),
        )
        c = _candidate(start=0.0, end=20.0)
        result = trim_trailing_transition(c, spans, clip_len=(10, 120))

        assert result["end"] == 15.0  # trim 'Moving on' sentence
        assert (result["end"] - result["start"]) >= 10  # duration OK

    def test_no_transition_marker_unchanged(self):
        """A clean ending sentence → candidate returned unchanged."""
        from producer.boundary_check import trim_trailing_transition

        spans = _spans(
            ("BPC-157 heals tendons rapidly.", 0.0, 10.0),
            ("Research shows 80% improvement.", 10.0, 20.0),
            ("That's the key takeaway.", 20.0, 30.0),
        )
        c = _candidate(start=0.0, end=30.0)
        result = trim_trailing_transition(c, spans, clip_len=(10, 120))

        assert result["end"] == 30.0
        assert result is not c or result["end"] == c["end"]

    def test_empty_spans_returns_original(self):
        """Empty sentence_spans → no-op."""
        from producer.boundary_check import trim_trailing_transition

        c = _candidate(start=0.0, end=30.0)
        result = trim_trailing_transition(c, [], clip_len=(10, 120))
        assert result is c  # exact same object (no copy made)

    def test_last_sentence_at_index_zero_noop(self):
        """When the transition sentence is index 0, cannot trim → no-op."""
        from producer.boundary_check import trim_trailing_transition

        spans = _spans(
            ("Number 1, first peptide discussion.", 0.0, 10.0),
        )
        c = _candidate(start=0.0, end=10.0)
        result = trim_trailing_transition(c, spans, clip_len=(5, 120))
        assert result["end"] == 10.0  # no-op

    def test_various_markers_all_trim(self):
        """Each listed transition marker triggers a trim."""
        from producer.boundary_check import trim_trailing_transition

        transition_sentences = [
            "Number 5, next topic.",
            "Next up, we cover this.",
            "The next one is interesting.",
            "Now again, back to this.",
            "And just like semaglutide.",
            "Also, another consideration.",
            "Oh, and one more thing.",
            "So the next question is.",
            "Moving on to the next subject.",
            "Alright next we have this.",
            "Alright, next on the list.",
        ]
        for sentence in transition_sentences:
            spans = _spans(
                ("Complete thought ends here.", 0.0, 20.0),
                (sentence, 20.0, 30.0),
            )
            c = _candidate(start=0.0, end=30.0)
            result = trim_trailing_transition(c, spans, clip_len=(10, 120))
            assert result["end"] == 20.0, (
                f"Expected trim for marker sentence {sentence!r}; got end={result['end']}"
            )

    def test_result_is_new_dict_not_mutated_original(self):
        """trim_trailing_transition returns a new dict; original is unmodified."""
        from producer.boundary_check import trim_trailing_transition

        spans = _spans(
            ("Substantive content.", 0.0, 20.0),
            ("Moving on to next topic.", 20.0, 30.0),
        )
        c = _candidate(start=0.0, end=30.0)
        result = trim_trailing_transition(c, spans, clip_len=(10, 120))

        assert result is not c
        assert c["end"] == 30.0  # original unchanged
        assert result["end"] == 20.0  # result trimmed


# ---------------------------------------------------------------------------
# apply_prefilters integration
# ---------------------------------------------------------------------------

class TestApplyPrefiltersTransitionTrim:

    def test_clip80_via_apply_prefilters(self):
        """clip-80 scenario: apply_prefilters trims the 'Number 16' sentence."""
        from producer.boundary_check import apply_prefilters

        spans = _spans(
            ("Mixed results. Some people have worse anxiety.", 220.0, 236.9),
            ("I'm not sure that I would try this one again.", 236.9, 238.4),
            ("Number 16, CAX. This is kind of like taking Adderall...", 238.4, 242.0),
        )
        # Candidate with end=238.9 (inside the 'Number 16' sentence)
        c = _candidate(start=220.0, end=238.9)
        result = apply_prefilters(c, spans, clip_len=(10, 120))

        assert result["end"] == 238.4, (
            f"apply_prefilters should trim to 238.4; got {result['end']}"
        )

    def test_consecutive_transitions_fully_trimmed(self):
        """Two transition sentences at the tail must BOTH be trimmed, not just
        the last one (reviewer issue #2)."""
        from producer.boundary_check import apply_prefilters

        spans = _spans(
            ("Selank works well for anxiety.", 200.0, 215.0),
            ("I'm not sure that I would try this one again.", 215.0, 225.0),
            ("Also, one more consideration to keep in mind.", 225.0, 232.0),
            ("Number 16, CAX. This is like Adderall...", 232.0, 240.0),
        )
        c = _candidate(start=200.0, end=239.0)
        result = apply_prefilters(c, spans, clip_len=(10, 120))
        # Both "Number 16" AND "Also," should be gone → end at 225.0
        assert result["end"] == 225.0, (
            f"both trailing transitions should trim to 225.0; got {result['end']}"
        )

    def test_normal_prefilter_unchanged_when_no_transition(self):
        """Prefilter behaviour unchanged when no transition sentence at end."""
        from producer.boundary_check import apply_prefilters

        spans = _spans(
            ("BPC-157 heals tendons.", 0.0, 10.0),
            ("Studies confirm improvement.", 10.0, 20.0),
            ("That is the key insight.", 20.0, 30.0),
        )
        c = _candidate(start=0.0, end=30.0)
        result = apply_prefilters(c, spans, clip_len=(10, 120))

        assert result["end"] == 30.0
        assert result["start"] == 0.0
