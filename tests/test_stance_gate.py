"""
tests/test_stance_gate.py — Unit tests for the campaign_alignment (R4 stance) gate.

Covers:
- aligned=true → pass (no didnt_pass from campaign_alignment)
- aligned=false → HARD didnt_pass (e.g. the nicotine-patch clip)
- Absent campaign_alignment field → back-compat PASS
- campaign_alignment NOT affected by relaxed_safety_checks
- stance text flows through _content_llm_call prompt
- stance injected in _build_prompt (ranker prompt stance block)

All LLM calls are mocked — no network calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clip(id=1, kind="clip", campaign="peptides", hook="test hook",
               source_id="yt:abc", start=10.0, end=40.0):
    clip = MagicMock()
    clip.id = id
    clip.kind = kind
    clip.campaign = campaign
    clip.hook = hook
    clip.source_id = source_id
    clip.start = start
    clip.end = end
    return clip


def _make_campaign_cfg(stance: str = "", relaxed: list | None = None):
    cfg = MagicMock()
    cfg.ranking.ranking_rules = "Default rules."
    cfg.ranking.stance = stance
    cfg.gate.relaxed_safety_checks = relaxed or []
    return cfg


_GOOD_SCORES = {k: 0.8 for k in [
    "hook_quality", "promise_delivery", "novelty", "pacing",
    "standalone_value", "speaker_engagement", "clean_ending",
    "shareability", "comprehension", "completion_likelihood",
]}

_SAFE_FLAGS = {
    "unsafe_diet_content": False,
    "medical_claims": False,
    "harmful_content": False,
    "guideline_violation": False,
}


# ---------------------------------------------------------------------------
# _score_content_verdict — campaign_alignment field
# ---------------------------------------------------------------------------

class TestScoreContentVerdictAlignment:
    def test_aligned_true_does_not_fail(self):
        from producer.review_gate import _score_content_verdict
        verdict = {
            "scores": _GOOD_SCORES.copy(),
            "safety": _SAFE_FLAGS.copy(),
            "campaign_alignment": {"aligned": True, "reason": "supports peptides"},
        }
        formula_score, reasons = _score_content_verdict(verdict)
        assert formula_score >= 0.6
        ca_reasons = [r for r in reasons if r["check"] == "campaign_alignment"]
        assert len(ca_reasons) == 1
        assert ca_reasons[0]["pass"] is True

    def test_aligned_false_hard_fail(self):
        """aligned=false is a HARD didnt_pass regardless of score."""
        from producer.review_gate import _score_content_verdict
        verdict = {
            "scores": _GOOD_SCORES.copy(),
            "safety": _SAFE_FLAGS.copy(),
            "campaign_alignment": {
                "aligned": False,
                "reason": "neither really WOWED him, nicotine patch beat them all",
            },
        }
        formula_score, reasons = _score_content_verdict(verdict)
        ca_reasons = [r for r in reasons if r["check"] == "campaign_alignment"]
        assert len(ca_reasons) == 1
        assert ca_reasons[0]["pass"] is False
        assert "CAMPAIGN ALIGNMENT FAIL" in ca_reasons[0]["reason"]

    def test_nicotine_patch_clip_fails_alignment(self):
        """The exact operator-rejected clip from the spec MUST fail."""
        from producer.review_gate import _score_content_verdict

        # Hook from the spec: "neither really WOWED him, a nicotine patch beat them all"
        verdict = {
            "scores": {k: 0.73 for k in _GOOD_SCORES},  # high scores otherwise
            "safety": _SAFE_FLAGS.copy(),
            "campaign_alignment": {
                "aligned": False,
                "reason": (
                    "Clip frames peptides as disappointing — "
                    "neither really WOWED him, a nicotine patch beat them all. "
                    "This contradicts the pro-peptide stance."
                ),
            },
        }
        _, reasons = _score_content_verdict(verdict)
        ca_reasons = [r for r in reasons if r["check"] == "campaign_alignment"]
        assert ca_reasons[0]["pass"] is False
        # Threshold check should also reflect the fail
        threshold_reasons = [r for r in reasons if r["check"] == "formula_score_threshold"]
        assert threshold_reasons[0]["pass"] is False

    def test_absent_alignment_field_is_pass(self):
        """No campaign_alignment field → PASS (back-compat)."""
        from producer.review_gate import _score_content_verdict
        verdict = {
            "scores": _GOOD_SCORES.copy(),
            "safety": _SAFE_FLAGS.copy(),
            # no campaign_alignment key
        }
        _, reasons = _score_content_verdict(verdict)
        ca_reasons = [r for r in reasons if r["check"] == "campaign_alignment"]
        assert len(ca_reasons) == 0  # field absent → no reason entry

    def test_alignment_fail_not_relaxable(self):
        """aligned=false fails EVEN WHEN relaxed_safety_checks includes safety keys."""
        from producer.review_gate import _score_content_verdict
        verdict = {
            "scores": _GOOD_SCORES.copy(),
            "safety": {"medical_claims": True, "unsafe_diet_content": False,
                       "harmful_content": False, "guideline_violation": False},
            "campaign_alignment": {
                "aligned": False,
                "reason": "anti-peptide framing",
            },
        }
        # Relaxing medical_claims should NOT rescue the alignment fail
        _, reasons = _score_content_verdict(
            verdict, relaxed_safety_checks=["medical_claims"]
        )
        ca_reasons = [r for r in reasons if r["check"] == "campaign_alignment"]
        assert ca_reasons[0]["pass"] is False


# ---------------------------------------------------------------------------
# _run_phase2 with stance — full integration through the gate
# ---------------------------------------------------------------------------

class TestRunPhase2WithStance:
    def _make_verdict(self, aligned: bool, formula: float = 0.8) -> dict:
        scores = {k: formula for k in _GOOD_SCORES}
        return {
            "scores": scores,
            "safety": _SAFE_FLAGS.copy(),
            "campaign_alignment": {
                "aligned": aligned,
                "reason": "nicotine patch beat them all" if not aligned else "pro-peptide",
            },
        }

    def test_aligned_clip_reaches_ready(self, monkeypatch):
        import producer.review_gate as rg
        from producer.review_gate import _run_phase2

        verdict = self._make_verdict(aligned=True)
        monkeypatch.setattr(rg, "_content_llm_call", lambda *a, **kw: verdict)

        clip = _make_clip()
        campaign_cfg = _make_campaign_cfg(stance="pro-peptide: ...")
        gate_status, fs, reasons = _run_phase2(clip, [], campaign_cfg, [])
        assert gate_status == "ready"

    def test_contradicting_clip_didnt_pass(self, monkeypatch):
        """The nicotine-patch hook MUST cause didnt_pass via campaign_alignment."""
        import producer.review_gate as rg
        from producer.review_gate import _run_phase2

        verdict = self._make_verdict(aligned=False)
        monkeypatch.setattr(rg, "_content_llm_call", lambda *a, **kw: verdict)

        clip = _make_clip(
            hook="neither really WOWED him, nicotine patch beat them all"
        )
        campaign_cfg = _make_campaign_cfg(
            stance="pro-peptide: content must present peptides positively"
        )
        gate_status, fs, reasons = _run_phase2(clip, [], campaign_cfg, [])
        assert gate_status == "didnt_pass"
        alignment_fail = any(
            not r["pass"] and r["check"] == "campaign_alignment"
            for r in reasons
        )
        assert alignment_fail is True

    def test_stance_passed_to_content_llm_call(self, monkeypatch):
        """Verify that stance is passed through to _content_llm_call."""
        import producer.review_gate as rg
        from producer.review_gate import _run_phase2

        captured_kwargs: dict = {}

        def fake_content_call(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return {
                "scores": _GOOD_SCORES.copy(),
                "safety": _SAFE_FLAGS.copy(),
                "campaign_alignment": {"aligned": True, "reason": "ok"},
            }

        monkeypatch.setattr(rg, "_content_llm_call", fake_content_call)

        clip = _make_clip()
        campaign_cfg = _make_campaign_cfg(stance="pro-peptide stance text here")
        _run_phase2(clip, [], campaign_cfg, [])

        assert "stance" in captured_kwargs
        assert "pro-peptide stance text here" in captured_kwargs["stance"]


# ---------------------------------------------------------------------------
# _build_prompt — stance block injection (ranker-side)
# ---------------------------------------------------------------------------

class TestBuildPromptStance:
    def test_stance_block_present_in_sentence_index_mode(self):
        from core.llm import _build_prompt

        spans = [
            {"text": "Hello world.", "start": 0.0, "end": 5.0},
            {"text": "Second sentence.", "start": 5.0, "end": 10.0},
        ]
        prompt = _build_prompt(
            transcript=[],
            rules="Some rules.",
            comment_summary=None,
            clip_len=(5, 60),
            max_clips=5,
            sentence_spans=spans,
            stance="pro-peptide: always positive",
        )
        assert "pro-peptide: always positive" in prompt
        assert "MANDATORY STANCE RULE" in prompt

    def test_stance_block_present_in_float_mode(self):
        from core.llm import _build_prompt

        transcript = [{"start": 0.0, "end": 10.0, "text": "Hello world."}]
        prompt = _build_prompt(
            transcript=transcript,
            rules="Some rules.",
            comment_summary=None,
            clip_len=(5, 60),
            max_clips=5,
            stance="pro-peptide: always positive",
        )
        assert "pro-peptide: always positive" in prompt
        assert "MANDATORY STANCE RULE" in prompt

    def test_no_stance_no_block_float_mode(self):
        from core.llm import _build_prompt

        transcript = [{"start": 0.0, "end": 10.0, "text": "Hello world."}]
        prompt = _build_prompt(
            transcript=transcript,
            rules="Some rules.",
            comment_summary=None,
            clip_len=(5, 60),
            max_clips=5,
            stance="",
        )
        assert "MANDATORY STANCE RULE" not in prompt

    def test_no_stance_no_block_sentence_mode(self):
        from core.llm import _build_prompt

        spans = [{"text": "Hello.", "start": 0.0, "end": 5.0}]
        prompt = _build_prompt(
            transcript=[],
            rules="Rules.",
            comment_summary=None,
            clip_len=(5, 60),
            max_clips=5,
            sentence_spans=spans,
            stance="",
        )
        assert "MANDATORY STANCE RULE" not in prompt

    def test_sentence_index_mode_when_spans_provided(self):
        """When sentence_spans are provided, the prompt uses start_sentence/end_sentence."""
        from core.llm import _build_prompt

        spans = [
            {"text": "First sentence.", "start": 0.0, "end": 5.0},
            {"text": "Second sentence.", "start": 5.0, "end": 10.0},
        ]
        prompt = _build_prompt(
            transcript=[],
            rules="Rules.",
            comment_summary=None,
            clip_len=(5, 60),
            max_clips=3,
            sentence_spans=spans,
        )
        assert "start_sentence" in prompt
        assert "end_sentence" in prompt
        assert "[0]" in prompt
        assert "[1]" in prompt

    def test_float_mode_when_no_spans(self):
        """Without sentence_spans, the prompt uses float-time format."""
        from core.llm import _build_prompt

        transcript = [{"start": 0.0, "end": 10.0, "text": "Hello world."}]
        prompt = _build_prompt(
            transcript=transcript,
            rules="Rules.",
            comment_summary=None,
            clip_len=(5, 60),
            max_clips=3,
        )
        assert '"start":' in prompt or '"start": ' in prompt
        assert "start_sentence" not in prompt


# ---------------------------------------------------------------------------
# Campaign config — stance field loaded from YAML
# ---------------------------------------------------------------------------

class TestPeptidesYamlStance:
    def test_peptides_yaml_has_stance(self):
        """The peptides campaign YAML must have a non-empty stance value."""
        from pathlib import Path
        from core.config import load_campaign

        yaml_path = Path("campaigns/peptides.yaml")
        if not yaml_path.exists():
            pytest.skip("peptides.yaml not found")
        cfg = load_campaign(yaml_path, strict_assets=False)
        assert cfg.ranking.stance, "peptides.yaml must have a non-empty stance"
        assert "pro-peptide" in cfg.ranking.stance.lower() or "positive" in cfg.ranking.stance.lower()

    def test_stance_field_default_empty_string(self):
        """RankingConfig.stance defaults to empty string."""
        from core.config import RankingConfig
        rc = RankingConfig()
        assert rc.stance == ""
