"""
tests/test_meme_classifier.py — unit tests for meme/classifier.py

Covers:
  - violates_hard_rules() pure function (em-dash detection)
  - _verdict_from_scores() pure function (threshold logic)
  - classify() with mocked LLM calls (no network)
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 1. violates_hard_rules — pure function, no mocking needed
# ---------------------------------------------------------------------------

class TestViolatesHardRules:
    """violates_hard_rules is a pure function; test all branches."""

    def test_em_dash_detected(self):
        from meme.classifier import violates_hard_rules

        result = violates_hard_rules("This is a great tip — really useful")
        assert len(result) == 1
        assert "em-dash" in result[0].lower()

    def test_unicode_em_dash_explicit(self):
        from meme.classifier import violates_hard_rules

        result = violates_hard_rules("Result—amazing")
        assert len(result) == 1

    def test_clean_text_returns_empty(self):
        from meme.classifier import violates_hard_rules

        result = violates_hard_rules("Here is a clean caption with no issues")
        assert result == []

    def test_double_hyphen_is_allowed(self):
        """A double hyphen '--' is NOT the same as an em-dash."""
        from meme.classifier import violates_hard_rules

        result = violates_hard_rules("Do this -- it works")
        assert result == []

    def test_regular_hyphen_is_allowed(self):
        from meme.classifier import violates_hard_rules

        result = violates_hard_rules("Low-carb diets can help")
        assert result == []

    def test_multiple_em_dashes(self):
        """Multiple em-dashes produce exactly one violation entry."""
        from meme.classifier import violates_hard_rules

        result = violates_hard_rules("First—Second—Third")
        assert len(result) == 1

    def test_empty_text_clean(self):
        from meme.classifier import violates_hard_rules

        assert violates_hard_rules("") == []


# ---------------------------------------------------------------------------
# 2. _verdict_from_scores — pure function (threshold logic)
# ---------------------------------------------------------------------------

class TestVerdictFromScores:
    """_verdict_from_scores applies threshold rules without any I/O."""

    def _call(self, scores: dict) -> tuple[str, list]:
        from meme.classifier import _verdict_from_scores
        return _verdict_from_scores(scores)

    def test_all_high_scores_pass(self):
        verdict, reasons = self._call({
            "on_format": 0.9,
            "on_voice": 0.85,
            "on_brand": 0.88,
            "legibility": 0.92,
            "compliance": 1.0,
        })
        assert verdict == "pass"
        assert reasons == []

    def test_compliance_zero_fails_immediately(self):
        verdict, reasons = self._call({
            "on_format": 0.99,
            "on_voice": 0.99,
            "on_brand": 0.99,
            "legibility": 0.99,
            "compliance": 0.0,
        })
        assert verdict == "fail"
        assert any("compliance" in r.lower() for r in reasons)

    def test_compliance_below_one_fails(self):
        verdict, reasons = self._call({
            "on_format": 0.9,
            "on_voice": 0.9,
            "on_brand": 0.9,
            "legibility": 0.9,
            "compliance": 0.9,  # not 1.0
        })
        assert verdict == "fail"

    def test_low_quality_mean_fails(self):
        """Mean of quality scores below PASS_THRESHOLD → fail."""
        verdict, reasons = self._call({
            "on_format": 0.4,
            "on_voice": 0.5,
            "on_brand": 0.6,
            "legibility": 0.5,
            "compliance": 1.0,
        })
        assert verdict == "fail"
        assert any("threshold" in r.lower() or "mean" in r.lower() for r in reasons)

    def test_exactly_at_threshold_passes(self):
        """Mean exactly equal to PASS_THRESHOLD (0.65) should pass."""
        from meme.classifier import PASS_THRESHOLD

        verdict, _ = self._call({
            "on_voice": PASS_THRESHOLD,
            "compliance": 1.0,
        })
        assert verdict == "pass"

    def test_just_below_threshold_fails(self):
        from meme.classifier import PASS_THRESHOLD

        verdict, _ = self._call({
            "on_voice": PASS_THRESHOLD - 0.01,
            "compliance": 1.0,
        })
        assert verdict == "fail"

    def test_text_only_voice_fail(self):
        """For text posts only on_voice is present; fails if voice < threshold."""
        verdict, reasons = self._call({
            "on_voice": 0.3,
            "compliance": 1.0,
        })
        assert verdict == "fail"

    def test_text_only_voice_pass(self):
        verdict, _ = self._call({
            "on_voice": 0.9,
            "compliance": 1.0,
        })
        assert verdict == "pass"

    def test_missing_compliance_defaults_to_pass(self):
        """Absent compliance key defaults to 1.0 (pass); quality checked normally."""
        verdict, _ = self._call({
            "on_voice": 0.9,
        })
        assert verdict == "pass"


# ---------------------------------------------------------------------------
# 3. classify() — mocked LLM calls
# ---------------------------------------------------------------------------

_GOOD_SCORES_JSON = """{
  "on_format": 0.9,
  "on_voice": 0.85,
  "on_brand": 0.88,
  "legibility": 0.92,
  "compliance": 1.0,
  "reasons": ["visually consistent", "voice matches profile"]
}"""

_COMPLIANCE_FAIL_JSON = """{
  "on_format": 0.9,
  "on_voice": 0.85,
  "on_brand": 0.88,
  "legibility": 0.92,
  "compliance": 0.0,
  "reasons": ["contains medical claim"]
}"""

_LOW_QUALITY_JSON = """{
  "on_format": 0.3,
  "on_voice": 0.4,
  "on_brand": 0.3,
  "legibility": 0.4,
  "compliance": 1.0,
  "reasons": ["off-brand image"]
}"""

_TEXT_ONLY_GOOD_JSON = """{
  "on_voice": 0.9,
  "compliance": 1.0,
  "reasons": ["voice matches perfectly"]
}"""

_SAMPLE_PROFILE: dict = {
    "aspect": "1:1",
    "visual_format": {
        "layout": "bold text on image",
        "image_style": "photography",
        "text_placement": "center",
        "colors": ["black", "white"],
        "typical_composition": "subject fills frame",
    },
    "caption_voice": {
        "tone": "direct",
        "person": "second",
        "sentence_length": "short",
        "punctuation_habits": "no periods",
        "slang_level": "low",
    },
    "measurable_rules": [
        {"rule": "caption under 10 words", "confidence": 0.9},
    ],
}


class TestClassify:
    """classify() with mocked LLM; verifies final ClassifierResult shape."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        """Ensure LLM env vars are set so require_llm() doesn't raise."""
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "claude-test")
        from core.settings import get_settings
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_classify_image_pass(self, monkeypatch):
        """Good scores → verdict 'pass', correct field shapes."""
        monkeypatch.setattr(
            "meme.classifier.call_vision",
            lambda *a, **kw: _GOOD_SCORES_JSON,
        )
        from meme.classifier import classify

        result = classify(b"\x89PNG", "Great caption", _SAMPLE_PROFILE)
        assert result["verdict"] == "pass"
        assert result["compliance"] == pytest.approx(1.0)
        assert result["on_voice"] >= 0.0
        assert result["on_format"] is not None

    def test_classify_compliance_fail(self, monkeypatch):
        """Compliance < 1.0 → verdict 'fail'."""
        monkeypatch.setattr(
            "meme.classifier.call_vision",
            lambda *a, **kw: _COMPLIANCE_FAIL_JSON,
        )
        from meme.classifier import classify

        result = classify(b"\x89PNG", "Clinically proven to work", _SAMPLE_PROFILE)
        assert result["verdict"] == "fail"
        assert result["compliance"] < 1.0

    def test_classify_low_quality_fail(self, monkeypatch):
        """Mean quality below threshold → verdict 'fail'."""
        monkeypatch.setattr(
            "meme.classifier.call_vision",
            lambda *a, **kw: _LOW_QUALITY_JSON,
        )
        from meme.classifier import classify

        result = classify(b"\x89PNG", "Off brand content", _SAMPLE_PROFILE)
        assert result["verdict"] == "fail"

    def test_classify_em_dash_skips_llm(self, monkeypatch):
        """Em-dash in caption → immediate fail without LLM call."""
        call_count = {"n": 0}

        def _mock_call(*a, **kw) -> str:
            call_count["n"] += 1
            return _GOOD_SCORES_JSON

        monkeypatch.setattr("meme.classifier.call_vision", _mock_call)
        monkeypatch.setattr("meme.classifier.call_text", _mock_call)
        from meme.classifier import classify

        result = classify(
            b"\x89PNG",
            "Great tip — do this now",
            _SAMPLE_PROFILE,
        )
        assert result["verdict"] == "fail"
        assert call_count["n"] == 0, "LLM should not be called for em-dash violations"
        assert result["compliance"] == pytest.approx(0.0)

    def test_classify_text_only_mode(self, monkeypatch):
        """image_data=None → text-only classifier (call_text, not call_vision)."""
        vision_calls = {"n": 0}
        text_calls = {"n": 0}

        def _mock_vision(*a, **kw) -> str:
            vision_calls["n"] += 1
            return _GOOD_SCORES_JSON

        def _mock_text(*a, **kw) -> str:
            text_calls["n"] += 1
            return _TEXT_ONLY_GOOD_JSON

        monkeypatch.setattr("meme.classifier.call_vision", _mock_vision)
        monkeypatch.setattr("meme.classifier.call_text", _mock_text)
        from meme.classifier import classify

        result = classify(None, "Good text post here", _SAMPLE_PROFILE)
        assert result["verdict"] == "pass"
        assert vision_calls["n"] == 0, "Vision should not be called for text-only"
        assert text_calls["n"] == 1
        assert result["on_format"] is None
        assert result["on_brand"] is None
        assert result["legibility"] is None

    def test_classify_returns_reasons_list(self, monkeypatch):
        """Result reasons is always a list, never None."""
        monkeypatch.setattr(
            "meme.classifier.call_vision",
            lambda *a, **kw: _GOOD_SCORES_JSON,
        )
        from meme.classifier import classify

        result = classify(b"\x89PNG", "Short caption", _SAMPLE_PROFILE)
        assert isinstance(result["reasons"], list)

    def test_classify_hard_rules_merged(self, monkeypatch):
        """Campaign-specific hard rules are passed to the classifier prompt."""
        prompts_seen = []

        def _capture_call(prompt, *a, **kw):
            prompts_seen.append(prompt)
            return _GOOD_SCORES_JSON

        monkeypatch.setattr("meme.classifier.call_vision", _capture_call)
        from meme.classifier import classify

        classify(
            b"\x89PNG",
            "Caption text",
            _SAMPLE_PROFILE,
            hard_rules=["No competitor brand mentions"],
        )
        assert prompts_seen, "LLM should have been called"
        assert "No competitor brand mentions" in prompts_seen[0]

    def test_parse_fail_defaults_to_fail_verdict(self, monkeypatch):
        """Unparseable LLM response → all scores 0.0 → verdict 'fail'."""
        monkeypatch.setattr(
            "meme.classifier.call_vision",
            lambda *a, **kw: "This is not valid JSON at all",
        )
        from meme.classifier import classify

        result = classify(b"\x89PNG", "Some caption", _SAMPLE_PROFILE)
        assert result["verdict"] == "fail"
