"""
tests/test_eval_segmentation.py — pytest wrapper for the eval harness (B4).

Invokes the core check functions from scripts/eval_segmentation.py directly,
so the same logic runs in CI (pytest) without spawning a subprocess.

All tests are OFFLINE — zero LLM, zero network, zero render spend.
"""

from __future__ import annotations

import json
import pathlib

import pytest

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "segmentation"
_PAIRS_FILE = _FIXTURES / "boundary_failure_pairs.json"


# ---------------------------------------------------------------------------
# Load pairs at module level so pytest can parametrize
# ---------------------------------------------------------------------------

def _load_pairs() -> list[dict]:
    with open(_PAIRS_FILE, encoding="utf-8") as fh:
        return json.load(fh).get("pairs", [])


_PAIRS = _load_pairs()


# ---------------------------------------------------------------------------
# Import the harness functions
# ---------------------------------------------------------------------------

from scripts.eval_segmentation import run_case, run_all_cases  # noqa: E402


# ---------------------------------------------------------------------------
# Per-case tests
# ---------------------------------------------------------------------------

class TestHardAssertions:
    """(a) and (b) must ALWAYS pass — these are deterministic, never need LLM."""

    @pytest.mark.parametrize("pair", _PAIRS, ids=[p["id"] for p in _PAIRS])
    def test_start_on_sentence_boundary(self, pair):
        """After guards, start must land on a sentence boundary."""
        result = run_case(pair)
        assert result["assertions"]["a_start_on_boundary"], (
            f"Case {pair['id']!r}: start not on sentence boundary. "
            f"Notes: {result['notes']}"
        )

    @pytest.mark.parametrize("pair", _PAIRS, ids=[p["id"] for p in _PAIRS])
    def test_end_on_sentence_boundary(self, pair):
        """After guards, end must land on a sentence boundary."""
        result = run_case(pair)
        assert result["assertions"]["b_end_on_boundary"], (
            f"Case {pair['id']!r}: end not on sentence boundary. "
            f"Notes: {result['notes']}"
        )

    @pytest.mark.parametrize("pair", _PAIRS, ids=[p["id"] for p in _PAIRS])
    def test_no_verdict_fail(self, pair):
        """No case may have verdict=FAIL (hard assertions must pass)."""
        result = run_case(pair)
        assert result["verdict"] != "FAIL", (
            f"Case {pair['id']!r} verdict is FAIL. Notes: {result['notes']}"
        )


class TestSoftAssertions:
    """(d) is deterministic for cases where transition markers are detectable."""

    @pytest.mark.parametrize("pair", _PAIRS, ids=[p["id"] for p in _PAIRS])
    def test_end_not_transition_opener(self, pair):
        """End sentence should not start with a list/topic transition marker."""
        result = run_case(pair)
        assert result["assertions"]["d_end_not_transition_opener"], (
            f"Case {pair['id']!r}: end sentence is a transition opener. "
            f"Notes: {result['notes']}"
        )


class TestHarnessStructure:
    """Structural tests for the harness output."""

    def test_all_cases_return_results(self):
        results = run_all_cases()
        assert len(results) == len(_PAIRS), "Expected one result per pair"

    def test_all_results_have_required_keys(self):
        results = run_all_cases()
        required = {"id", "wrong", "correct", "after_guards", "assertions", "verdict", "notes"}
        for r in results:
            assert required.issubset(r.keys()), f"Missing keys in result: {r['id']}"

    def test_all_ids_match_pairs(self):
        results = run_all_cases()
        pair_ids = {p["id"] for p in _PAIRS}
        result_ids = {r["id"] for r in results}
        assert pair_ids == result_ids

    def test_after_guards_is_populated(self):
        results = run_all_cases()
        for r in results:
            assert r["after_guards"] is not None, f"after_guards is None for {r['id']}"
            assert "start" in r["after_guards"]
            assert "end" in r["after_guards"]

    def test_guards_produce_sentence_aligned_output(self):
        """Guards must land both start and end on sentence boundaries for all cases."""
        results = run_all_cases()
        for r in results:
            a = r["assertions"]
            assert a["a_start_on_boundary"] and a["b_end_on_boundary"], (
                f"Case {r['id']!r}: guard output not sentence-aligned "
                f"(a={a['a_start_on_boundary']}, b={a['b_end_on_boundary']}). "
                f"Notes: {r['notes']}"
            )

    def test_honest_limitations_documented(self):
        """Cases where deterministic guards cannot fully reach correct boundaries
        should still produce PASS (all hard assertions) not FAIL."""
        # F2_cycling_lr3: correct start is a 'But' sentence, which is_bad_start
        # rightfully rejects. The LLM context is needed to prefer this opening.
        # F3, F1, F4: LLM semantic unit split needed for exact end trimming.
        # All should PASS (hard assertions) not FAIL.
        results = run_all_cases()
        for r in results:
            assert r["verdict"] in ("PASS", "PARTIAL"), (
                f"Case {r['id']!r} has verdict FAIL — hard assertions broken. "
                f"Notes: {r['notes']}"
            )
