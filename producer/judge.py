"""
producer/judge.py — Deterministic judge for rendered clips (add-video pipeline).

Contract (docs/ADD_VIDEO_CONTRACTS.md §4):

  judge(report: CriticReport, attempts_used: int, max_corrections: int = 2)
      -> JudgeDecision

PURE function: no LLM, no I/O, no DB.  Same input always gives same output.
Called ONCE per clip, after the correction loop ends (pass, terminal, or bound hit).

Decision rules:
  report.passed                              → approved
  any failure with severity='terminal'
    AND check in SAFETY_SET                  → rejected
  any failure with severity='terminal'
    (non-safety)                             → escalate_to_human
  attempts_used > max_corrections
    (loop bound hit, failures remain)        → escalate_to_human

DB mapping (reuses existing UI):
  approved          → gate_status='ready',       status stays 'pending_review'
  rejected          → gate_status='didnt_pass',  reasons prefixed "SAFETY"
  escalate_to_human → gate_status='didnt_pass',  reasons + attempt count

The SAFETY set equals the four safety checks from review_gate minus any that
were campaign-relaxed (relaxed checks never reach the judge as failures because
_score_content_verdict already marks them as PASS).
"""

from __future__ import annotations

from datetime import datetime, timezone

from producer.pipeline_contracts import CriticReport, JudgeDecision

# ---------------------------------------------------------------------------
# Safety set — checks that warrant an outright REJECTED verdict.
# Matches the four safety auto-fail keys in review_gate.py.
# Relaxed checks never appear as failures in a CriticReport (they are already
# marked as PASS by _score_content_verdict) so the full set is listed here.
# ---------------------------------------------------------------------------

SAFETY_SET: frozenset[str] = frozenset({
    "safety_unsafe_diet_content",
    "safety_medical_claims",
    "safety_harmful_content",
    "safety_guideline_violation",
})


def judge(
    report: CriticReport,
    attempts_used: int,
    max_corrections: int = 2,
) -> JudgeDecision:
    """Deterministic terminal decision for a clip after the critic loop ends.

    Args:
        report:          CriticReport from the final critic run.
        attempts_used:   How many correction re-renders have been done for this
                         clip (0 = no corrections, 1 = one correction, etc.).
        max_corrections: Hard loop bound (default 2; hard-coded to 2 in contract).

    Returns:
        JudgeDecision — deterministic for identical inputs.
    """
    clip_id = report.clip_id
    decided_at = datetime.now(tz=timezone.utc).isoformat()

    # Rule 1: report.passed → approved
    if report.passed:
        return JudgeDecision(
            clip_id=clip_id,
            decision="approved",
            reasons=["All critic checks passed"],
            decided_at=decided_at,
        )

    failures = report.failures  # may be empty only when passed=True (handled above)

    # Rule 2: any terminal safety failure → rejected
    safety_failures = [
        f for f in failures
        if f.severity == "terminal" and f.check in SAFETY_SET
    ]
    if safety_failures:
        reasons = [
            f"SAFETY: {f.reason}" for f in safety_failures
        ]
        return JudgeDecision(
            clip_id=clip_id,
            decision="rejected",
            reasons=reasons,
            decided_at=decided_at,
        )

    # Rule 3: any terminal non-safety failure → escalate_to_human
    terminal_failures = [f for f in failures if f.severity == "terminal"]
    if terminal_failures:
        reasons = [f.reason for f in terminal_failures]
        reasons.append(f"Corrections attempted: {attempts_used}")
        return JudgeDecision(
            clip_id=clip_id,
            decision="escalate_to_human",
            reasons=reasons,
            decided_at=decided_at,
        )

    # Rule 4: loop bound hit (correctable failures remain but max corrections reached)
    if attempts_used >= max_corrections:
        reasons = [f.reason for f in failures]
        reasons.append(
            f"Correction loop bound reached ({attempts_used}/{max_corrections} corrections)"
        )
        return JudgeDecision(
            clip_id=clip_id,
            decision="escalate_to_human",
            reasons=reasons,
            decided_at=decided_at,
        )

    # Fallback: escalate for any other unexpected state
    reasons = [f.reason for f in failures] if failures else ["Unknown failure state"]
    return JudgeDecision(
        clip_id=clip_id,
        decision="escalate_to_human",
        reasons=reasons,
        decided_at=decided_at,
    )


def apply_judge_to_clip(
    clip_row: object,
    decision: JudgeDecision,
    session: object,
) -> None:
    """Persist a JudgeDecision to the clip row and set gate_status accordingly.

    gate_status mapping per §4:
      approved          → 'ready'
      rejected          → 'didnt_pass'
      escalate_to_human → 'didnt_pass'

    Does NOT commit — caller owns the transaction.
    """
    gate_status_map = {
        "approved": "ready",
        "rejected": "didnt_pass",
        "escalate_to_human": "didnt_pass",
    }
    new_gate_status = gate_status_map.get(decision.decision, "didnt_pass")

    clip_row.gate_status = new_gate_status  # type: ignore[attr-defined]
    clip_row.judge_decision = decision.model_dump()  # type: ignore[attr-defined]

    # For rejected clips prefix the reasons list in gate_reasons so the human
    # reviewer sees the SAFETY label in the Didn't-pass card.
    existing_reasons = getattr(clip_row, "gate_reasons", None) or []
    judge_reasons: list[dict] = [
        {"phase": "judge", "check": "judge", "pass": decision.decision == "approved",
         "reason": r}
        for r in decision.reasons
    ]
    clip_row.gate_reasons = list(existing_reasons) + judge_reasons  # type: ignore[attr-defined]
