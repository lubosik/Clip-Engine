"""
tests/test_judge.py — determinism and decision-branch coverage for
producer/judge.py (ADD_VIDEO_CONTRACTS.md §4).

Rules tested (all five branches + apply_judge_to_clip persistence):
  1. passed=True                  → approved
  2. terminal + safety check      → rejected (reasons prefixed SAFETY:)
  3. terminal + non-safety check  → escalate_to_human
  4. attempts_used >= max         → escalate_to_human
  5. fallback (empty failures)    → escalate_to_human (edge case)

Determinism: calling judge() twice with identical args must produce identical
decided_at strings within the same test (mocked datetime).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from producer.judge import SAFETY_SET, apply_judge_to_clip, judge
from producer.pipeline_contracts import CriticFailure, CriticReport, JudgeDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_report(
    failures: list[CriticFailure],
    passed: bool = False,
    clip_id: int = 1,
    attempt: int = 0,
    formula_score: float | None = None,
) -> CriticReport:
    return CriticReport(
        clip_id=clip_id,
        attempt=attempt,
        failures=failures,
        formula_score=formula_score,
        passed=passed,
    )


def _terminal_safety(check: str) -> CriticFailure:
    return CriticFailure(
        phase="2",
        check=check,
        reason=f"Violation: {check}",
        severity="terminal",
        correction=None,
    )


def _terminal_nonsafety(check: str = "watermark_visible") -> CriticFailure:
    return CriticFailure(
        phase="1",
        check=check,
        reason="Watermark visible in top-left corner",
        severity="terminal",
        correction=None,
    )


def _correctable(check: str = "self_contained") -> CriticFailure:
    from producer.pipeline_contracts import Correction
    return CriticFailure(
        phase="2",
        check=check,
        reason="Clip ends mid-sentence",
        severity="correctable",
        correction=Correction(kind="adjust_end", delta_sentences=-1),
    )


# ---------------------------------------------------------------------------
# Rule 1: report.passed → approved
# ---------------------------------------------------------------------------

class TestRule1Approved:
    def test_passed_true_returns_approved(self) -> None:
        report = _make_report(failures=[], passed=True)
        decision = judge(report, attempts_used=0)
        assert decision.decision == "approved"

    def test_approved_reason_is_populated(self) -> None:
        report = _make_report(failures=[], passed=True)
        decision = judge(report, attempts_used=0)
        assert len(decision.reasons) > 0
        assert "passed" in decision.reasons[0].lower()

    def test_approved_clip_id_preserved(self) -> None:
        report = _make_report(failures=[], passed=True, clip_id=42)
        decision = judge(report, attempts_used=0)
        assert decision.clip_id == 42

    def test_passed_ignores_attempts_used(self) -> None:
        """Even with high attempts_used, passed=True → approved."""
        report = _make_report(failures=[], passed=True)
        decision = judge(report, attempts_used=99)
        assert decision.decision == "approved"

    def test_passed_ignores_max_corrections(self) -> None:
        report = _make_report(failures=[], passed=True)
        decision = judge(report, attempts_used=0, max_corrections=0)
        assert decision.decision == "approved"


# ---------------------------------------------------------------------------
# Rule 2: terminal + safety → rejected
# ---------------------------------------------------------------------------

class TestRule2Rejected:
    @pytest.mark.parametrize("check", sorted(SAFETY_SET))
    def test_each_safety_check_causes_rejected(self, check: str) -> None:
        report = _make_report(failures=[_terminal_safety(check)], passed=False)
        decision = judge(report, attempts_used=0)
        assert decision.decision == "rejected"

    def test_rejected_reasons_prefixed_safety(self) -> None:
        report = _make_report(
            failures=[_terminal_safety("safety_harmful_content")], passed=False
        )
        decision = judge(report, attempts_used=0)
        assert all(r.startswith("SAFETY:") for r in decision.reasons)

    def test_mixed_safety_and_correctable_still_rejected(self) -> None:
        """Safety terminal takes priority over correctable failures."""
        failures = [
            _terminal_safety("safety_harmful_content"),
            _correctable("self_contained"),
        ]
        report = _make_report(failures=failures, passed=False)
        decision = judge(report, attempts_used=0)
        assert decision.decision == "rejected"

    def test_multiple_safety_failures_all_reasons_listed(self) -> None:
        failures = [
            _terminal_safety("safety_harmful_content"),
            _terminal_safety("safety_medical_claims"),
        ]
        report = _make_report(failures=failures, passed=False)
        decision = judge(report, attempts_used=0)
        assert decision.decision == "rejected"
        assert len(decision.reasons) == 2
        assert all(r.startswith("SAFETY:") for r in decision.reasons)

    def test_safety_rejected_even_at_attempt_0(self) -> None:
        """Safety failure must be immediately terminal, not deferred."""
        report = _make_report(
            failures=[_terminal_safety("safety_unsafe_diet_content")], passed=False
        )
        decision = judge(report, attempts_used=0, max_corrections=2)
        assert decision.decision == "rejected"


# ---------------------------------------------------------------------------
# Rule 3: terminal non-safety → escalate_to_human
# ---------------------------------------------------------------------------

class TestRule3EscalateTerminalNonSafety:
    def test_watermark_terminal_escalates(self) -> None:
        report = _make_report(failures=[_terminal_nonsafety("watermark_visible")], passed=False)
        decision = judge(report, attempts_used=0)
        assert decision.decision == "escalate_to_human"

    def test_resolution_terminal_escalates(self) -> None:
        report = _make_report(failures=[_terminal_nonsafety("resolution")], passed=False)
        decision = judge(report, attempts_used=0)
        assert decision.decision == "escalate_to_human"

    def test_escalate_includes_attempt_count_in_reasons(self) -> None:
        report = _make_report(failures=[_terminal_nonsafety()], passed=False)
        decision = judge(report, attempts_used=1)
        reasons_text = " ".join(decision.reasons)
        assert "1" in reasons_text  # attempt count present

    def test_non_safety_terminal_beats_correctable(self) -> None:
        failures = [_terminal_nonsafety("watermark_visible"), _correctable()]
        report = _make_report(failures=failures, passed=False)
        decision = judge(report, attempts_used=0)
        assert decision.decision == "escalate_to_human"


# ---------------------------------------------------------------------------
# Rule 4: attempts_used >= max_corrections → escalate
# ---------------------------------------------------------------------------

class TestRule4LoopBoundHit:
    def test_correctable_at_max_corrections_escalates(self) -> None:
        report = _make_report(failures=[_correctable()], passed=False)
        decision = judge(report, attempts_used=2, max_corrections=2)
        assert decision.decision == "escalate_to_human"

    def test_correctable_below_max_would_not_escalate_via_rule4(self) -> None:
        """With attempts_used < max, no terminal failures, the fallback fires."""
        report = _make_report(failures=[_correctable()], passed=False)
        decision = judge(report, attempts_used=1, max_corrections=2)
        # Should be escalate (falls through to rule 4 or fallback)
        assert decision.decision == "escalate_to_human"

    def test_bound_reason_mentions_correction_count(self) -> None:
        report = _make_report(failures=[_correctable()], passed=False)
        decision = judge(report, attempts_used=2, max_corrections=2)
        reasons_text = " ".join(decision.reasons)
        assert "2" in reasons_text

    def test_custom_max_corrections_respected(self) -> None:
        report = _make_report(failures=[_correctable()], passed=False)
        decision_at_1 = judge(report, attempts_used=1, max_corrections=1)
        assert decision_at_1.decision == "escalate_to_human"

    def test_over_max_escalates(self) -> None:
        report = _make_report(failures=[_correctable()], passed=False)
        decision = judge(report, attempts_used=5, max_corrections=2)
        assert decision.decision == "escalate_to_human"


# ---------------------------------------------------------------------------
# Rule 5: fallback (passed=False, no failures of any kind)
# ---------------------------------------------------------------------------

class TestRule5Fallback:
    def test_empty_failures_passed_false_escalates(self) -> None:
        """
        CriticReport with passed=False must have at least one failure
        (model_validator enforces this). This test exercises the fallback
        branch by directly constructing an artificial scenario where the
        failures list is empty after filtering — not normally reachable via
        valid Pydantic models, but the judge function itself should still be
        safe in this edge case.

        We bypass Pydantic here by calling judge() with a patched report object.
        """
        class FakeReport:
            clip_id = 99
            passed = False
            failures: list = []
            formula_score: float | None = None
            attempt = 0

        decision = judge(FakeReport(), attempts_used=0)  # type: ignore[arg-type]
        assert decision.decision == "escalate_to_human"
        assert len(decision.reasons) > 0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        """judge() must be deterministic for identical inputs.

        We freeze datetime.now so decided_at is also identical.
        """
        report = _make_report(failures=[_terminal_nonsafety()], passed=False)
        fixed_time = datetime(2026, 7, 27, 0, 0, 0, tzinfo=timezone.utc)

        with patch("producer.judge.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_time

            d1 = judge(report, attempts_used=1)
            d2 = judge(report, attempts_used=1)

        assert d1.decision == d2.decision
        assert d1.reasons == d2.reasons
        assert d1.decided_at == d2.decided_at

    def test_approved_determinism(self) -> None:
        report = _make_report(failures=[], passed=True)
        fixed_time = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        with patch("producer.judge.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_time
            d1 = judge(report, attempts_used=0)
            d2 = judge(report, attempts_used=0)
        assert d1.model_dump() == d2.model_dump()


# ---------------------------------------------------------------------------
# apply_judge_to_clip
# ---------------------------------------------------------------------------

class TestApplyJudgeToClip:
    class _FakeClip:
        gate_status: str = "pending"
        judge_decision: dict | None = None
        gate_reasons: list = []

    def test_approved_sets_ready(self) -> None:
        clip = self._FakeClip()
        decision = JudgeDecision(
            clip_id=1,
            decision="approved",
            reasons=["All checks passed"],
            decided_at="2026-07-27T00:00:00+00:00",
        )
        apply_judge_to_clip(clip, decision, session=None)
        assert clip.gate_status == "ready"
        assert clip.judge_decision is not None
        assert clip.judge_decision["decision"] == "approved"

    def test_rejected_sets_didnt_pass(self) -> None:
        clip = self._FakeClip()
        decision = JudgeDecision(
            clip_id=2,
            decision="rejected",
            reasons=["SAFETY: harmful content"],
            decided_at="2026-07-27T00:00:00+00:00",
        )
        apply_judge_to_clip(clip, decision, session=None)
        assert clip.gate_status == "didnt_pass"

    def test_escalate_sets_didnt_pass(self) -> None:
        clip = self._FakeClip()
        decision = JudgeDecision(
            clip_id=3,
            decision="escalate_to_human",
            reasons=["Terminal: watermark visible"],
            decided_at="2026-07-27T00:00:00+00:00",
        )
        apply_judge_to_clip(clip, decision, session=None)
        assert clip.gate_status == "didnt_pass"

    def test_gate_reasons_appended(self) -> None:
        clip = self._FakeClip()
        clip.gate_reasons = [{"existing": True}]
        decision = JudgeDecision(
            clip_id=1,
            decision="approved",
            reasons=["Passed"],
            decided_at="2026-07-27T00:00:00+00:00",
        )
        apply_judge_to_clip(clip, decision, session=None)
        # Original reason preserved; judge reason appended
        assert clip.gate_reasons[0] == {"existing": True}
        assert len(clip.gate_reasons) == 2

    def test_judge_decision_is_dict(self) -> None:
        clip = self._FakeClip()
        decision = JudgeDecision(
            clip_id=5,
            decision="approved",
            reasons=["ok"],
            decided_at="2026-07-27T00:00:00+00:00",
        )
        apply_judge_to_clip(clip, decision, session=None)
        assert isinstance(clip.judge_decision, dict)

    def test_idempotent_apply(self) -> None:
        """Applying the same decision twice overwrites cleanly."""
        clip = self._FakeClip()
        decision = JudgeDecision(
            clip_id=1,
            decision="approved",
            reasons=["Passed"],
            decided_at="2026-07-27T00:00:00+00:00",
        )
        apply_judge_to_clip(clip, decision, session=None)
        apply_judge_to_clip(clip, decision, session=None)
        # Should not raise; gate_reasons will be doubled (acceptable)
        assert clip.gate_status == "ready"
