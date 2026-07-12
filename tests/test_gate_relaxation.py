"""Tests for per-campaign safety relaxation in the review gate (GateConfig)."""

from __future__ import annotations

import pytest

from producer.review_gate import _score_content_verdict


def _verdict(medical=False, harmful=False, scores=0.8):
    return {
        "scores": {k: scores for k in [
            "hook_quality", "promise_delivery", "novelty", "pacing",
            "standalone_value", "speaker_engagement", "clean_ending",
            "shareability", "comprehension", "completion_likelihood",
        ]},
        "safety": {
            "unsafe_diet_content": False,
            "medical_claims": medical,
            "harmful_content": harmful,
            "guideline_violation": False,
        },
    }


def _safety_reason(reasons, key):
    return next(r for r in reasons if r["check"] == f"safety_{key}")


class TestRelaxedSafetyChecks:
    def test_strict_default_fails_on_medical_claims(self):
        score, reasons = _score_content_verdict(_verdict(medical=True))
        r = _safety_reason(reasons, "medical_claims")
        assert r["pass"] is False
        assert "AUTO-FAIL" in r["reason"]
        threshold = next(x for x in reasons if x["check"] == "formula_score_threshold")
        assert threshold["pass"] is False  # safety fail blocks even a good score

    def test_relaxed_medical_claims_does_not_fail(self):
        score, reasons = _score_content_verdict(
            _verdict(medical=True), relaxed_safety_checks=["medical_claims"]
        )
        r = _safety_reason(reasons, "medical_claims")
        assert r["pass"] is True
        assert "RELAXED" in r["reason"]  # flag preserved for the human reviewer
        threshold = next(x for x in reasons if x["check"] == "formula_score_threshold")
        assert threshold["pass"] is True

    def test_relaxation_is_scoped_other_checks_stay_strict(self):
        score, reasons = _score_content_verdict(
            _verdict(medical=True, harmful=True),
            relaxed_safety_checks=["medical_claims"],
        )
        assert _safety_reason(reasons, "medical_claims")["pass"] is True
        assert _safety_reason(reasons, "harmful_content")["pass"] is False
        threshold = next(x for x in reasons if x["check"] == "formula_score_threshold")
        assert threshold["pass"] is False

    def test_untriggered_relaxed_check_reads_ok(self):
        score, reasons = _score_content_verdict(
            _verdict(medical=False), relaxed_safety_checks=["medical_claims"]
        )
        r = _safety_reason(reasons, "medical_claims")
        assert r["pass"] is True
        assert "Safety OK" in r["reason"]

    def test_low_score_still_fails_despite_relaxation(self):
        score, reasons = _score_content_verdict(
            _verdict(medical=True, scores=0.3),
            relaxed_safety_checks=["medical_claims"],
        )
        threshold = next(x for x in reasons if x["check"] == "formula_score_threshold")
        assert threshold["pass"] is False


class TestGateConfig:
    def test_valid_config(self):
        from core.config import GateConfig

        cfg = GateConfig(relaxed_safety_checks=["medical_claims"])
        assert cfg.relaxed_safety_checks == ["medical_claims"]

    def test_default_is_strict(self):
        from core.config import GateConfig

        assert GateConfig().relaxed_safety_checks == []

    def test_unknown_check_rejected(self):
        from core.config import GateConfig

        with pytest.raises(Exception):
            GateConfig(relaxed_safety_checks=["not_a_check"])

    def test_peptides_yaml_relaxes_medical_claims(self):
        from core.config import load_campaign

        cfg = load_campaign("campaigns/peptides.yaml", strict_assets=False)
        assert cfg.gate.relaxed_safety_checks == ["medical_claims"]
        assert cfg.sources.youtube.results_per_search == 10
        assert cfg.sources.skip_discovery_backlog == 20

    def test_fitness_yaml_stays_strict(self):
        from core.config import load_campaign

        cfg = load_campaign("campaigns/fitness.yaml", strict_assets=False)
        assert cfg.gate.relaxed_safety_checks == []
