"""
tests/test_critic.py — unit coverage for producer/critic.py.

Uses module-level monkeypatching of _critic_run_phase1 and _critic_run_phase2
so no real LLM / network calls are made.

Coverage areas (per ADD_VIDEO_CONTRACTS.md §3):
  - Phase-1 mapping table: each check → correct (severity, correction) pair
  - Correctable failure: rerender/adjust/rewrite_hook corrections
  - Terminal severity: watermark, resolution, safety checks
  - CriticUnavailable raised on phase-1 / phase-2 transport errors
  - Zero-context: prior_failures passed through; no ranker reason injected
  - run_critic happy path (both phases pass → CriticReport.passed=True)
  - run_critic phase-1 fail short-circuits (no phase-2 called)
  - run_critic phase-2 failure produces CriticFailures
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

import producer.critic as _critic_mod
from producer.critic import (
    CriticUnavailable,
    SAFETY_CHECKS,
    TERMINAL_VISION_CHECKS,
    _correction_for_phase1_failure,
    _phase2_reason_to_critic_failure,
    run_critic,
)
from producer.pipeline_contracts import (
    Correction,
    CriticFailure,
    CriticReport,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clip(
    clip_id: int = 1,
    hook: str = "Incredible hook here",
    start: float = 10.0,
    end: float = 70.0,
    campaign: str = "testcamp",
    correction_attempts: int = 0,
) -> Any:
    return SimpleNamespace(
        id=clip_id,
        hook=hook,
        start=start,
        end=end,
        campaign=campaign,
        correction_attempts=correction_attempts,
    )


def _make_campaign_cfg(
    ranking_rules: str = "Prefer actionable moments.",
    relaxed_safety_checks: list | None = None,
    stance: str = "",
) -> Any:
    ranking = SimpleNamespace(
        ranking_rules=ranking_rules,
        stance=stance,
    )
    gate = SimpleNamespace(relaxed_safety_checks=relaxed_safety_checks or [])
    return SimpleNamespace(ranking=ranking, gate=gate, name="testcamp")


# Phase-1 returns: (reasons, phase_passed, probe_data)
def _p1_pass() -> tuple:
    return [], True, {}


def _p1_fail(check: str, reason: str = "failed") -> tuple:
    return [{"check": check, "reason": reason, "pass": False}], False, {}


def _p1_pass_with_reasons(reasons: list) -> tuple:
    """Phase 1 passes overall but returns some non-fail reasons."""
    return reasons, True, {}


# Phase-2 returns: (gate_status, formula_score, reasons)
def _p2_pass() -> tuple:
    return "ready", 0.78, []


def _p2_fail_check(check: str, reason: str = "failed") -> tuple:
    return "didnt_pass", 0.45, [{"check": check, "reason": reason, "pass": False, "phase": "2"}]


# ---------------------------------------------------------------------------
# _correction_for_phase1_failure mapping table
# ---------------------------------------------------------------------------

class TestCorrectionForPhase1Failure:
    """Verify the deterministic phase-1 → (severity, Correction) mapping."""

    @pytest.mark.parametrize("check", sorted(TERMINAL_VISION_CHECKS))
    def test_terminal_vision_checks_are_terminal(self, check: str) -> None:
        severity, correction = _correction_for_phase1_failure(check, "reason")
        assert severity == "terminal"
        assert correction is None

    @pytest.mark.parametrize("check", sorted(SAFETY_CHECKS))
    def test_safety_checks_are_terminal(self, check: str) -> None:
        severity, correction = _correction_for_phase1_failure(check, "reason")
        assert severity == "terminal"
        assert correction is None

    @pytest.mark.parametrize("check", [
        "hook_present_in_hook_frame",
        "hook_absent_in_mid_frame",
        "captions_present",
    ])
    def test_overlay_checks_are_correctable_rerender(self, check: str) -> None:
        severity, correction = _correction_for_phase1_failure(check, "reason")
        assert severity == "correctable"
        assert correction is not None
        assert correction.kind == "rerender"

    def test_unknown_check_is_terminal(self) -> None:
        severity, correction = _correction_for_phase1_failure("completely_unknown_check", "r")
        assert severity == "terminal"
        assert correction is None


# ---------------------------------------------------------------------------
# _phase2_reason_to_critic_failure
# ---------------------------------------------------------------------------

class TestPhase2ReasonToCriticFailure:
    def _reason(self, check: str, reason: str = "reason") -> dict:
        return {"check": check, "reason": reason, "pass": False, "phase": "2"}

    def test_passing_reason_returns_none(self) -> None:
        reason = {"check": "hook_quality", "reason": "fine", "pass": True}
        result = _phase2_reason_to_critic_failure(reason, {})
        assert result is None

    @pytest.mark.parametrize("check", sorted(SAFETY_CHECKS))
    def test_safety_checks_are_terminal(self, check: str) -> None:
        cf = _phase2_reason_to_critic_failure(self._reason(check), {})
        assert cf is not None
        assert cf.severity == "terminal"
        assert cf.check == check

    def test_campaign_alignment_terminal(self) -> None:
        cf = _phase2_reason_to_critic_failure(self._reason("campaign_alignment"), {})
        assert cf is not None
        assert cf.severity == "terminal"

    def test_topical_relevance_terminal(self) -> None:
        cf = _phase2_reason_to_critic_failure(self._reason("topical_relevance"), {})
        assert cf is not None
        assert cf.severity == "terminal"

    def test_formula_score_threshold_terminal(self) -> None:
        cf = _phase2_reason_to_critic_failure(self._reason("formula_score_threshold"), {})
        assert cf is not None
        assert cf.severity == "terminal"

    def test_hook_body_match_correctable_rewrite(self) -> None:
        verdict = {"hook_body_match": {"matches": False, "suggested_hook": "Better hook"}}
        cf = _phase2_reason_to_critic_failure(self._reason("hook_body_match"), verdict)
        assert cf is not None
        assert cf.severity == "correctable"
        assert cf.correction is not None
        assert cf.correction.kind == "rewrite_hook"
        assert cf.correction.new_hook == "Better hook"

    def test_hook_body_match_no_suggested_hook(self) -> None:
        verdict = {"hook_body_match": {"matches": False}}
        cf = _phase2_reason_to_critic_failure(self._reason("hook_body_match"), verdict)
        assert cf is not None
        assert cf.correction is not None
        assert cf.correction.new_hook is None  # No suggestion from LLM

    def test_self_contained_ends_on_new_topic_adjust_end(self) -> None:
        verdict = {"self_contained": {"complete_thought": False, "ends_on_new_topic": True}}
        cf = _phase2_reason_to_critic_failure(self._reason("self_contained"), verdict)
        assert cf is not None
        assert cf.severity == "correctable"
        assert cf.correction is not None
        assert cf.correction.kind == "adjust_end"
        assert cf.correction.delta_sentences == -1

    def test_self_contained_starts_mid_thought_adjust_start(self) -> None:
        verdict = {"self_contained": {"complete_thought": False, "ends_on_new_topic": False}}
        cf = _phase2_reason_to_critic_failure(self._reason("self_contained"), verdict)
        assert cf is not None
        assert cf.severity == "correctable"
        assert cf.correction is not None
        assert cf.correction.kind == "adjust_start"
        assert cf.correction.delta_sentences == -1

    @pytest.mark.parametrize("rubric_check", [
        "hook_quality", "promise_delivery", "novelty", "pacing",
        "standalone_value", "speaker_engagement", "clean_ending",
        "shareability", "comprehension", "completion_likelihood",
    ])
    def test_individual_rubric_scores_return_none(self, rubric_check: str) -> None:
        """Individual rubric failures are handled by aggregate formula_score_threshold."""
        cf = _phase2_reason_to_critic_failure(self._reason(rubric_check), {})
        assert cf is None

    def test_unknown_check_is_terminal(self) -> None:
        cf = _phase2_reason_to_critic_failure(self._reason("some_random_check"), {})
        assert cf is not None
        assert cf.severity == "terminal"


# ---------------------------------------------------------------------------
# run_critic — happy path (both phases pass)
# ---------------------------------------------------------------------------

class TestRunCriticHappyPath:
    def test_both_phases_pass_report_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_critic_mod, "_critic_run_phase1", lambda *a, **kw: _p1_pass())
        monkeypatch.setattr(_critic_mod, "_critic_run_phase2", lambda *a, **kw: _p2_pass())

        # Patch _score_content_verdict and _build helpers to avoid import errors
        with patch("producer.review_gate._build_transcript_slice", return_value="text"), \
             patch("producer.review_gate._build_lookahead_slice", return_value=""), \
             patch("producer.review_gate._score_content_verdict", return_value=(0.8, [])), \
             patch("producer.critic._content_llm_call_with_corrections", return_value={}):
            report = run_critic(
                _make_clip(),
                "r2://bucket/clip.mp4",
                [{"start": 0.0, "end": 1.0, "text": "hello world"}],
                _make_campaign_cfg(),
                session=None,
            )

        assert report.passed is True
        assert len(report.failures) == 0
        assert report.clip_id == 1

    def test_formula_score_populated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_critic_mod, "_critic_run_phase1", lambda *a, **kw: _p1_pass())

        with patch("producer.review_gate._build_transcript_slice", return_value="t"), \
             patch("producer.review_gate._build_lookahead_slice", return_value=""), \
             patch("producer.review_gate._score_content_verdict", return_value=(0.75, [])), \
             patch("producer.critic._content_llm_call_with_corrections", return_value={}):
            report = run_critic(
                _make_clip(),
                "/local/clip.mp4",
                None,
                _make_campaign_cfg(),
                session=None,
            )

        assert report.formula_score == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# run_critic — phase-1 failure short-circuits (no phase-2)
# ---------------------------------------------------------------------------

class TestRunCriticPhase1Fail:
    def test_phase1_terminal_failure_no_phase2_called(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        phase2_called = []

        def fake_p2(*args: Any, **kwargs: Any) -> tuple:
            phase2_called.append(True)
            return _p2_pass()

        monkeypatch.setattr(
            _critic_mod, "_critic_run_phase1",
            lambda *a, **kw: _p1_fail("watermark_visible", "Watermark in frame"),
        )
        monkeypatch.setattr(_critic_mod, "_critic_run_phase2", fake_p2)

        with patch("producer.review_gate._build_transcript_slice", return_value="t"), \
             patch("producer.review_gate._build_lookahead_slice", return_value=""), \
             patch("producer.review_gate._score_content_verdict", return_value=(0.0, [])), \
             patch("producer.critic._content_llm_call_with_corrections", return_value={}):
            report = run_critic(
                _make_clip(),
                "/local/clip.mp4",
                None,
                _make_campaign_cfg(),
                session=None,
            )

        assert phase2_called == []  # Phase 2 must NOT be called
        assert report.passed is False
        assert any(f.check == "watermark_visible" for f in report.failures)

    def test_phase1_correctable_failure_no_phase2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A correctable overlay failure in phase-1 also short-circuits phase-2."""
        phase2_calls = []
        monkeypatch.setattr(
            _critic_mod, "_critic_run_phase1",
            lambda *a, **kw: _p1_fail("captions_present", "No captions found"),
        )
        monkeypatch.setattr(
            _critic_mod, "_critic_run_phase2",
            lambda *a, **kw: phase2_calls.append(True) or _p2_pass(),
        )

        with patch("producer.review_gate._build_transcript_slice", return_value="t"), \
             patch("producer.review_gate._build_lookahead_slice", return_value=""), \
             patch("producer.review_gate._score_content_verdict", return_value=(0.0, [])), \
             patch("producer.critic._content_llm_call_with_corrections", return_value={}):
            report = run_critic(
                _make_clip(),
                "/local/clip.mp4",
                None,
                _make_campaign_cfg(),
                session=None,
            )

        assert phase2_calls == []
        assert report.passed is False
        f = next(f for f in report.failures if f.check == "captions_present")
        assert f.severity == "correctable"
        assert f.correction is not None and f.correction.kind == "rerender"


# ---------------------------------------------------------------------------
# run_critic — phase-2 failure produces CriticFailures
# ---------------------------------------------------------------------------

class TestRunCriticPhase2Failures:
    def test_self_contained_failure_in_phase2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_critic_mod, "_critic_run_phase1", lambda *a, **kw: _p1_pass())

        verdict = {"self_contained": {"complete_thought": False, "ends_on_new_topic": True}}
        p2_reasons = [{"check": "self_contained", "reason": "ends mid-topic", "pass": False, "phase": "2"}]

        with patch("producer.review_gate._build_transcript_slice", return_value="t"), \
             patch("producer.review_gate._build_lookahead_slice", return_value=""), \
             patch("producer.review_gate._score_content_verdict", return_value=(0.55, p2_reasons)), \
             patch("producer.critic._content_llm_call_with_corrections", return_value=verdict):
            report = run_critic(
                _make_clip(),
                "/local/clip.mp4",
                None,
                _make_campaign_cfg(),
                session=None,
            )

        assert report.passed is False
        sc_failure = next(f for f in report.failures if f.check == "self_contained")
        assert sc_failure.severity == "correctable"
        assert sc_failure.correction is not None
        assert sc_failure.correction.kind == "adjust_end"

    def test_safety_failure_in_phase2_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_critic_mod, "_critic_run_phase1", lambda *a, **kw: _p1_pass())

        safety_check = "safety_harmful_content"
        p2_reasons = [{"check": safety_check, "reason": "Harmful content", "pass": False, "phase": "2"}]

        with patch("producer.review_gate._build_transcript_slice", return_value="t"), \
             patch("producer.review_gate._build_lookahead_slice", return_value=""), \
             patch("producer.review_gate._score_content_verdict", return_value=(0.0, p2_reasons)), \
             patch("producer.critic._content_llm_call_with_corrections", return_value={}):
            report = run_critic(
                _make_clip(),
                "/local/clip.mp4",
                None,
                _make_campaign_cfg(),
                session=None,
            )

        assert report.passed is False
        safety_f = next(f for f in report.failures if f.check == safety_check)
        assert safety_f.severity == "terminal"
        assert safety_f.correction is None


# ---------------------------------------------------------------------------
# CriticUnavailable — phase-1 transport error
# ---------------------------------------------------------------------------

class TestCriticUnavailablePhase1:
    def test_phase1_exception_raises_critic_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_p1(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("Network down")

        monkeypatch.setattr(_critic_mod, "_critic_run_phase1", fail_p1)

        with pytest.raises(CriticUnavailable, match="Phase 1"):
            run_critic(
                _make_clip(),
                "/local/clip.mp4",
                None,
                _make_campaign_cfg(),
                session=None,
            )


# ---------------------------------------------------------------------------
# CriticUnavailable — phase-2 transport error
# ---------------------------------------------------------------------------

class TestCriticUnavailablePhase2:
    def test_content_llm_error_raises_critic_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_critic_mod, "_critic_run_phase1", lambda *a, **kw: _p1_pass())

        with patch("producer.review_gate._build_transcript_slice", return_value="t"), \
             patch("producer.review_gate._build_lookahead_slice", return_value=""), \
             patch(
                 "producer.critic._content_llm_call_with_corrections",
                 side_effect=CriticUnavailable("timeout"),
             ):
            with pytest.raises(CriticUnavailable, match="timeout"):
                run_critic(
                    _make_clip(),
                    "/local/clip.mp4",
                    None,
                    _make_campaign_cfg(),
                    session=None,
                )


# ---------------------------------------------------------------------------
# Zero-context: prior_failures passed but no ranker reason injected
# ---------------------------------------------------------------------------

class TestZeroContext:
    """Verify that preference_context is always empty and prior_failures
    is passed through the call chain — no ranker reasoning leaked."""

    def test_preference_context_not_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_content_llm_call_with_corrections must be called with
        preference_context='' regardless of prior_failures presence."""
        captured_kwargs: dict = {}

        def capture_call(*args: Any, **kwargs: Any) -> dict:
            captured_kwargs.update(kwargs)
            captured_kwargs["args"] = args
            return {}

        monkeypatch.setattr(_critic_mod, "_critic_run_phase1", lambda *a, **kw: _p1_pass())

        prior = [
            CriticFailure(
                phase="2",
                check="self_contained",
                reason="ended mid-topic",
                severity="correctable",
                correction=Correction(kind="adjust_end", delta_sentences=-1),
            )
        ]

        with patch("producer.review_gate._build_transcript_slice", return_value="t"), \
             patch("producer.review_gate._build_lookahead_slice", return_value=""), \
             patch("producer.review_gate._score_content_verdict", return_value=(0.8, [])), \
             patch(
                 "producer.critic._content_llm_call_with_corrections",
                 side_effect=capture_call,
             ):
            run_critic(
                _make_clip(),
                "/local/clip.mp4",
                None,
                _make_campaign_cfg(),
                session=None,
                prior_failures=prior,
            )

        assert captured_kwargs.get("preference_context", None) == ""

    def test_prior_failures_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """prior_failures must be forwarded to _content_llm_call_with_corrections."""
        captured_prior: list = []

        def capture_call(*args: Any, **kwargs: Any) -> dict:
            captured_prior.extend(kwargs.get("prior_failures") or [])
            return {}

        monkeypatch.setattr(_critic_mod, "_critic_run_phase1", lambda *a, **kw: _p1_pass())

        prior = [
            CriticFailure(
                phase="2",
                check="hook_body_match",
                reason="hook does not match body",
                severity="correctable",
                correction=Correction(kind="rewrite_hook", new_hook="New hook"),
            )
        ]

        with patch("producer.review_gate._build_transcript_slice", return_value="t"), \
             patch("producer.review_gate._build_lookahead_slice", return_value=""), \
             patch("producer.review_gate._score_content_verdict", return_value=(0.8, [])), \
             patch(
                 "producer.critic._content_llm_call_with_corrections",
                 side_effect=capture_call,
             ):
            run_critic(
                _make_clip(),
                "/local/clip.mp4",
                None,
                _make_campaign_cfg(),
                session=None,
                prior_failures=prior,
            )

        assert len(captured_prior) == 1
        assert captured_prior[0].check == "hook_body_match"
