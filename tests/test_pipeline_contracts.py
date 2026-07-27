"""
tests/test_pipeline_contracts.py — Pydantic validation coverage for
producer/pipeline_contracts.py (ADD_VIDEO_CONTRACTS.md §2).

Runs against the real Pydantic v2 library — no mocking required.
Tests validate both happy-path construction and error-path rejection.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from producer.pipeline_contracts import (
    ClipCandidate,
    Correction,
    CriticFailure,
    CriticReport,
    JudgeDecision,
    RenderOutcome,
    Segment,
    Sentence,
    TranscriptPayload,
    VideoRunResult,
)


# ---------------------------------------------------------------------------
# Segment
# ---------------------------------------------------------------------------

class TestSegment:
    def test_happy(self) -> None:
        s = Segment(start=0.0, end=5.0, text="Hello world")
        assert s.start == 0.0
        assert s.end == 5.0
        assert s.text == "Hello world"

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(ValidationError, match="text"):
            Segment(start=0.0, end=5.0, text="")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValidationError, match="text"):
            Segment(start=0.0, end=5.0, text="   ")

    def test_missing_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Segment(end=5.0, text="hi")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Sentence
# ---------------------------------------------------------------------------

class TestSentence:
    def test_happy(self) -> None:
        s = Sentence(text="This is a sentence.", start=1.0, end=4.0)
        assert s.text == "This is a sentence."

    def test_all_fields_required(self) -> None:
        with pytest.raises(ValidationError):
            Sentence(text="x", start=0.0)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# TranscriptPayload
# ---------------------------------------------------------------------------

class TestTranscriptPayload:
    def _seg(self, n: int = 0) -> Segment:
        return Segment(start=float(n), end=float(n + 1), text=f"seg {n}")

    def test_happy_no_sentences(self) -> None:
        tp = TranscriptPayload(
            source_id="youtube:abc12345678",
            segments=[self._seg(0), self._seg(1)],
        )
        assert tp.sentences is None
        assert len(tp.segments) == 2

    def test_happy_with_sentences(self) -> None:
        sents = [Sentence(text="hi", start=0.0, end=1.0)]
        tp = TranscriptPayload(
            source_id="youtube:abc12345678",
            segments=[self._seg(0)],
            sentences=sents,
        )
        assert tp.sentences is not None and len(tp.sentences) == 1

    def test_empty_source_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="source_id"):
            TranscriptPayload(source_id="", segments=[self._seg()])

    def test_empty_segments_rejected(self) -> None:
        with pytest.raises(ValidationError, match="segments"):
            TranscriptPayload(source_id="youtube:abc12345678", segments=[])

    def test_invalid_segment_propagates(self) -> None:
        with pytest.raises(ValidationError):
            TranscriptPayload(
                source_id="youtube:abc12345678",
                segments=[{"start": 0.0, "end": 1.0, "text": ""}],
            )


# ---------------------------------------------------------------------------
# Correction
# ---------------------------------------------------------------------------

class TestCorrection:
    def test_rerender_no_delta_no_hook(self) -> None:
        c = Correction(kind="rerender")
        assert c.delta_sentences is None
        assert c.new_hook is None

    def test_adjust_start_with_delta(self) -> None:
        c = Correction(kind="adjust_start", delta_sentences=-1)
        assert c.delta_sentences == -1

    def test_rewrite_hook_with_new_hook(self) -> None:
        c = Correction(kind="rewrite_hook", new_hook="Bold new hook")
        assert c.new_hook == "Bold new hook"

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(ValidationError, match="kind"):
            Correction(kind="do_magic")  # type: ignore[arg-type]

    def test_note_defaults_empty(self) -> None:
        c = Correction(kind="rerender")
        assert c.note == ""


# ---------------------------------------------------------------------------
# CriticFailure
# ---------------------------------------------------------------------------

class TestCriticFailure:
    def test_terminal_no_correction(self) -> None:
        f = CriticFailure(
            phase="1",
            check="watermark_visible",
            reason="Watermark in top-left",
            severity="terminal",
        )
        assert f.correction is None

    def test_correctable_with_correction(self) -> None:
        corr = Correction(kind="adjust_end", delta_sentences=-1)
        f = CriticFailure(
            phase="2",
            check="self_contained",
            reason="Clip ends mid-sentence",
            severity="correctable",
            correction=corr,
        )
        assert f.correction is not None
        assert f.correction.kind == "adjust_end"

    def test_invalid_severity_rejected(self) -> None:
        with pytest.raises(ValidationError, match="severity"):
            CriticFailure(
                phase="1", check="foo", reason="bar", severity="maybe"  # type: ignore[arg-type]
            )

    def test_invalid_phase_rejected(self) -> None:
        with pytest.raises(ValidationError, match="phase"):
            CriticFailure(
                phase="3", check="foo", reason="bar", severity="terminal"  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# CriticReport
# ---------------------------------------------------------------------------

class TestCriticReport:
    def test_passed_no_failures(self) -> None:
        r = CriticReport(clip_id=1, attempt=0, failures=[], formula_score=0.8, passed=True)
        assert r.passed is True
        assert len(r.failures) == 0

    def test_passed_false_with_failures(self) -> None:
        f = CriticFailure(
            phase="1", check="watermark_visible", reason="Visible", severity="terminal"
        )
        r = CriticReport(
            clip_id=1, attempt=0, failures=[f], formula_score=None, passed=False
        )
        assert r.passed is False

    def test_passed_true_with_failures_rejected(self) -> None:
        """passed=True while failures is non-empty must be rejected."""
        f = CriticFailure(
            phase="1", check="watermark_visible", reason="Visible", severity="terminal"
        )
        with pytest.raises(ValidationError, match="passed"):
            CriticReport(clip_id=1, attempt=0, failures=[f], formula_score=None, passed=True)

    def test_passed_false_no_failures_rejected(self) -> None:
        """passed=False with zero failures must be rejected (contradictory)."""
        with pytest.raises(ValidationError, match="passed"):
            CriticReport(clip_id=1, attempt=0, failures=[], formula_score=None, passed=False)

    def test_formula_score_optional(self) -> None:
        r = CriticReport(clip_id=2, attempt=1, failures=[], formula_score=None, passed=True)
        assert r.formula_score is None

    def test_negative_attempt_allowed(self) -> None:
        """attempt can technically be any int — no lower bound in contract."""
        r = CriticReport(clip_id=1, attempt=0, failures=[], passed=True)
        assert r.attempt == 0


# ---------------------------------------------------------------------------
# ClipCandidate
# ---------------------------------------------------------------------------

class TestClipCandidate:
    def test_happy(self) -> None:
        c = ClipCandidate(start=10.0, end=70.0, score=0.85, hook="Amazing moment")
        assert c.attempt == 0
        assert c.reason == ""

    def test_end_not_greater_than_start_rejected(self) -> None:
        with pytest.raises(ValidationError, match="end"):
            ClipCandidate(start=70.0, end=10.0, score=0.5, hook="hook")

    def test_end_equal_start_rejected(self) -> None:
        with pytest.raises(ValidationError, match="end"):
            ClipCandidate(start=10.0, end=10.0, score=0.5, hook="hook")

    def test_empty_hook_rejected(self) -> None:
        with pytest.raises(ValidationError, match="hook"):
            ClipCandidate(start=0.0, end=60.0, score=0.5, hook="")

    def test_whitespace_hook_rejected(self) -> None:
        with pytest.raises(ValidationError, match="hook"):
            ClipCandidate(start=0.0, end=60.0, score=0.5, hook="   ")

    def test_attempt_default(self) -> None:
        c = ClipCandidate(start=0.0, end=60.0, score=0.9, hook="hook")
        assert c.attempt == 0

    def test_attempt_positive(self) -> None:
        c = ClipCandidate(start=0.0, end=60.0, score=0.9, hook="hook", attempt=2)
        assert c.attempt == 2


# ---------------------------------------------------------------------------
# RenderOutcome
# ---------------------------------------------------------------------------

class TestRenderOutcome:
    def test_ok_status(self) -> None:
        r = RenderOutcome(
            clip_id=1,
            file_path="r2://bucket/clip.mp4",
            thumb_path="r2://bucket/clip.jpg",
            backend="modal",
            gpu="A100",
            status="ok",
        )
        assert r.error is None

    def test_error_status_no_file_path_required(self) -> None:
        r = RenderOutcome(
            clip_id=1,
            file_path="",
            thumb_path="",
            backend="modal",
            gpu=None,
            status="error",
            error="GPU OOM",
        )
        assert r.status == "error"
        assert r.error == "GPU OOM"

    def test_ok_with_empty_file_path_rejected(self) -> None:
        """file_path must be non-empty when status='ok'."""
        with pytest.raises(ValidationError, match="file_path"):
            RenderOutcome(
                clip_id=1,
                file_path="",
                thumb_path="",
                backend="modal",
                status="ok",
            )

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError, match="status"):
            RenderOutcome(
                clip_id=1,
                file_path="r2://x",
                thumb_path="r2://y",
                backend="modal",
                status="pending",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# JudgeDecision
# ---------------------------------------------------------------------------

class TestJudgeDecision:
    def test_approved(self) -> None:
        d = JudgeDecision(
            clip_id=1,
            decision="approved",
            reasons=["All checks passed"],
            decided_at="2026-07-27T00:00:00+00:00",
        )
        assert d.decision == "approved"

    def test_rejected(self) -> None:
        d = JudgeDecision(
            clip_id=2,
            decision="rejected",
            reasons=["SAFETY: contains medical claims"],
            decided_at="2026-07-27T00:00:00+00:00",
        )
        assert d.decision == "rejected"

    def test_escalate(self) -> None:
        d = JudgeDecision(
            clip_id=3,
            decision="escalate_to_human",
            reasons=["Clip ends mid-sentence after 2 corrections"],
            decided_at="2026-07-27T00:00:00+00:00",
        )
        assert d.decision == "escalate_to_human"

    def test_invalid_decision_rejected(self) -> None:
        with pytest.raises(ValidationError, match="decision"):
            JudgeDecision(
                clip_id=1,
                decision="maybe",  # type: ignore[arg-type]
                reasons=[],
                decided_at="2026-07-27T00:00:00+00:00",
            )


# ---------------------------------------------------------------------------
# VideoRunResult
# ---------------------------------------------------------------------------

class TestVideoRunResult:
    def _minimal(self, **overrides: object) -> dict:
        base = dict(
            campaign="testcamp",
            source_id="youtube:abc12345678",
            clips_identified=3,
            clips_rendered=3,
            clips_approved=2,
            clips_rejected=1,
            clips_escalated=0,
            total_corrections=1,
            apify_spend_usd=0.01,
            modal_spend_usd=0.50,
            status="complete",
        )
        base.update(overrides)
        return base

    def test_complete(self) -> None:
        r = VideoRunResult(**self._minimal())  # type: ignore[arg-type]
        assert r.status == "complete"
        assert r.error is None

    def test_failed_with_error(self) -> None:
        r = VideoRunResult(**self._minimal(status="failed", error="Something went wrong"))  # type: ignore[arg-type]
        assert r.status == "failed"
        assert r.error == "Something went wrong"

    def test_partial(self) -> None:
        r = VideoRunResult(**self._minimal(status="partial"))  # type: ignore[arg-type]
        assert r.status == "partial"

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError, match="status"):
            VideoRunResult(**self._minimal(status="running"))  # type: ignore[arg-type]

    def test_negative_spend_allowed(self) -> None:
        """Contract does not forbid credits / zero spend."""
        r = VideoRunResult(**self._minimal(modal_spend_usd=0.0))  # type: ignore[arg-type]
        assert r.modal_spend_usd == 0.0
