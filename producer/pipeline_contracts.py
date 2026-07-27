"""
producer/pipeline_contracts.py — Typed handoff contracts for the add-video pipeline.

Every orchestrator stage validates its input/output with these Pydantic v2
models.  model_validate errors → clip/video failed state, logged.

Binding per docs/ADD_VIDEO_CONTRACTS.md §2.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator


# ---------------------------------------------------------------------------
# Low-level building blocks
# ---------------------------------------------------------------------------

class Segment(BaseModel):
    """A single transcript segment."""
    start: float
    end: float
    text: str

    @field_validator("text")
    @classmethod
    def text_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Segment text must not be empty")
        return v


class Sentence(BaseModel):
    """A punctuation-restored sentence span."""
    text: str
    start: float
    end: float


# ---------------------------------------------------------------------------
# §2 Contract models
# ---------------------------------------------------------------------------

class TranscriptPayload(BaseModel):
    """Validated transcript handed to the pipeline orchestrator."""
    source_id: str
    segments: list[Segment]
    sentences: list[Sentence] | None = None

    @field_validator("source_id")
    @classmethod
    def source_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("TranscriptPayload source_id must not be empty")
        return v

    @field_validator("segments")
    @classmethod
    def segments_non_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("TranscriptPayload segments must not be empty")
        return v


class Correction(BaseModel):
    """Machine-usable correction instruction from the critic."""
    kind: Literal["adjust_start", "adjust_end", "rewrite_hook", "rerender"]
    # adjust_*: delta in whole sentences (int, ±1..3) relative to current bounds
    delta_sentences: int | None = None
    new_hook: str | None = None  # rewrite_hook only
    note: str = ""


class CriticFailure(BaseModel):
    """A single failure identified by the critic."""
    phase: Literal["1", "2"]
    check: str
    reason: str  # plain language, shown to the human
    severity: Literal["correctable", "terminal"]
    correction: Correction | None = None  # None when terminal / not correctable


class CriticReport(BaseModel):
    """Full report from one critic run on a rendered clip.

    The critic NEVER sets a terminal clip status — it only produces this report.
    The judge, called once per clip after the correction loop ends, determines
    the terminal decision.
    """
    clip_id: int
    attempt: int  # which render attempt was inspected (0-indexed)
    failures: list[CriticFailure]  # empty list = clean pass
    formula_score: float | None = None
    passed: bool  # convenience: True iff failures is empty

    @model_validator(mode="after")
    def passed_matches_failures(self) -> "CriticReport":
        # Enforce consistency — passed must equal (len(failures) == 0).
        # Callers that pass contradictory data receive a ValidationError so the
        # inconsistency surfaces immediately instead of propagating silently.
        expected = len(self.failures) == 0
        if self.passed != expected:
            raise ValueError(
                f"CriticReport.passed ({self.passed!r}) does not match "
                f"len(failures) == 0 ({expected!r}). "
                "Set passed=True only when failures is empty."
            )
        return self


class ClipCandidate(BaseModel):
    """A clip candidate passed between pipeline stages."""
    start: float
    end: float
    score: float  # 0..1
    hook: str
    reason: str = ""
    # Carried through corrections — 0 = first render, 1 = first correction, etc.
    attempt: int = 0

    @field_validator("hook")
    @classmethod
    def hook_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("ClipCandidate hook must not be empty")
        return v

    @model_validator(mode="after")
    def end_after_start(self) -> "ClipCandidate":
        if self.end <= self.start:
            raise ValueError(
                f"ClipCandidate end ({self.end}) must be > start ({self.start})"
            )
        return self


class RenderOutcome(BaseModel):
    """Result of a single render attempt."""
    clip_id: int
    file_path: str  # r2:// or local path; non-empty when status='ok'
    thumb_path: str
    backend: str
    gpu: str | None = None
    status: Literal["ok", "error"]
    error: str | None = None

    @model_validator(mode="after")
    def file_path_non_empty_on_ok(self) -> "RenderOutcome":
        if self.status == "ok" and not self.file_path.strip():
            raise ValueError("RenderOutcome file_path must be non-empty when status='ok'")
        return self


class JudgeDecision(BaseModel):
    """Terminal decision produced by the judge (pure function, no LLM)."""
    clip_id: int
    decision: Literal["approved", "rejected", "escalate_to_human"]
    reasons: list[str]  # plain language
    decided_at: str  # iso8601


class VideoRunResult(BaseModel):
    """Summary returned by run_video()."""
    campaign: str
    source_id: str
    clips_identified: int
    clips_rendered: int
    clips_approved: int    # judge → approved
    clips_rejected: int    # judge → rejected
    clips_escalated: int   # judge → escalate_to_human
    total_corrections: int
    apify_spend_usd: float
    modal_spend_usd: float
    status: Literal["complete", "failed", "partial"]
    error: str | None = None
