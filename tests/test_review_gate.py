"""
tests/test_review_gate.py — Unit tests for producer/review_gate.py.

LLM/vision calls are mocked via monkeypatch.  No network calls are made here.
Tests cover:
  - GateResult plumbing (gate_status, gate_reasons, formula_score)
  - 'pending' on transport errors (infra unavailable — never fails the clip)
  - 'didnt_pass' on animation detection (Phase 1 auto-fail)
  - 'didnt_pass' on low formula_score
  - 'didnt_pass' on safety flag
  - 'ready' on all-pass scenario
  - Meme clips are always 'pending' (skip the video gate)
  - Empty video_path_or_r2 returns 'pending'
  - JSON parsing helpers
  - Content scoring: formula_score average, safety detection
  - Transcript slice builder
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — minimal fake objects
# ---------------------------------------------------------------------------

def _make_clip(
    id=1,
    kind="clip",
    campaign="fitness",
    hook="This is a hook",
    source_id="youtube:abc",
    start=10.0,
    end=40.0,
):
    """Return a minimal namespace object mimicking a Clip ORM row."""
    clip = MagicMock()
    clip.id = id
    clip.kind = kind
    clip.campaign = campaign
    clip.hook = hook
    clip.source_id = source_id
    clip.start = start
    clip.end = end
    return clip


def _make_campaign_cfg(ranking_rules="Be informative and engaging."):
    cfg = MagicMock()
    cfg.ranking.ranking_rules = ranking_rules
    return cfg


def _make_probe(width=1080, height=1920, video_dur=30.0, fmt_dur=30.0):
    """Return a fake ffprobe output dict."""
    return {
        "streams": [{
            "codec_type": "video",
            "width": width,
            "height": height,
            "duration": str(video_dur),
        }],
        "format": {"duration": str(fmt_dur)},
    }


def _good_vision_verdict():
    return {
        "hook_present_in_hook_frame": True,
        "hook_absent_in_mid_frame": True,
        "captions_present": True,
        "watermark_visible": True,
        "real_humans": True,
        "speaker_centered": True,
        "animation_detected": False,
    }


def _good_content_verdict(score=0.75):
    return {
        "scores": {
            "hook_quality": score,
            "promise_delivery": score,
            "novelty": score,
            "pacing": score,
            "standalone_value": score,
            "speaker_engagement": score,
            "clean_ending": score,
            "shareability": score,
            "comprehension": score,
            "completion_likelihood": score,
        },
        "safety": {
            "unsafe_diet_content": False,
            "medical_claims": False,
            "harmful_content": False,
            "guideline_violation": False,
        },
    }


# ---------------------------------------------------------------------------
# JSON parsing helper
# ---------------------------------------------------------------------------

class TestParseJsonObject:
    def test_plain_json(self):
        from producer.review_gate import _parse_json_object
        result = _parse_json_object('{"a": 1, "b": true}')
        assert result == {"a": 1, "b": True}

    def test_json_in_prose(self):
        from producer.review_gate import _parse_json_object
        text = 'Here is the result:\n{"ok": true}\nDone.'
        assert _parse_json_object(text) == {"ok": True}

    def test_code_fence_stripped(self):
        from producer.review_gate import _parse_json_object
        text = '```json\n{"x": 42}\n```'
        assert _parse_json_object(text) == {"x": 42}

    def test_raises_on_no_json(self):
        from producer.review_gate import _parse_json_object
        with pytest.raises(ValueError, match="No JSON object"):
            _parse_json_object("No JSON here at all.")


# ---------------------------------------------------------------------------
# Content scoring
# ---------------------------------------------------------------------------

class TestScoreContentVerdict:
    def test_all_pass_high_score(self):
        from producer.review_gate import _score_content_verdict
        formula_score, reasons = _score_content_verdict(_good_content_verdict(0.8))
        assert abs(formula_score - 0.8) < 1e-6
        safety_reasons = [r for r in reasons if r["check"].startswith("safety_")]
        assert all(r["pass"] for r in safety_reasons)

    def test_low_score_threshold(self):
        from producer.review_gate import _score_content_verdict, FORMULA_SCORE_THRESHOLD
        # Score below threshold
        formula_score, reasons = _score_content_verdict(_good_content_verdict(0.4))
        assert formula_score < FORMULA_SCORE_THRESHOLD
        threshold_reason = next(
            r for r in reasons if r["check"] == "formula_score_threshold"
        )
        assert not threshold_reason["pass"]

    def test_safety_flag_sets_reason(self):
        from producer.review_gate import _score_content_verdict
        verdict = _good_content_verdict(0.8)
        verdict["safety"]["unsafe_diet_content"] = True
        _, reasons = _score_content_verdict(verdict)
        diet_reason = next(r for r in reasons if r["check"] == "safety_unsafe_diet_content")
        assert not diet_reason["pass"]
        assert "AUTO-FAIL" in diet_reason["reason"]

    def test_formula_score_is_average_of_10_scores(self):
        from producer.review_gate import _score_content_verdict
        # 5 scores at 1.0, 5 scores at 0.0 → average 0.5
        verdict = {
            "scores": {k: (1.0 if i < 5 else 0.0) for i, k in enumerate([
                "hook_quality", "promise_delivery", "novelty", "pacing",
                "standalone_value", "speaker_engagement", "clean_ending",
                "shareability", "comprehension", "completion_likelihood",
            ])},
            "safety": {k: False for k in [
                "unsafe_diet_content", "medical_claims", "harmful_content", "guideline_violation"
            ]},
        }
        formula_score, _ = _score_content_verdict(verdict)
        assert abs(formula_score - 0.5) < 1e-6


# ---------------------------------------------------------------------------
# Transcript slice builder
# ---------------------------------------------------------------------------

class TestBuildTranscriptSlice:
    def test_returns_placeholder_when_no_segments(self):
        from producer.review_gate import _build_transcript_slice
        result = _build_transcript_slice(None, 0.0, 30.0)
        assert "not available" in result.lower()

    def test_filters_by_time_range(self):
        from producer.review_gate import _build_transcript_slice
        segs = [
            {"start": 0, "end": 5, "text": "early"},
            {"start": 10, "end": 20, "text": "in range"},
            {"start": 50, "end": 60, "text": "late"},
        ]
        result = _build_transcript_slice(segs, 8.0, 25.0)
        assert "in range" in result
        assert "early" not in result
        assert "late" not in result

    def test_returns_all_when_no_range(self):
        from producer.review_gate import _build_transcript_slice
        segs = [{"start": 0, "end": 5, "text": "a"}, {"start": 5, "end": 10, "text": "b"}]
        result = _build_transcript_slice(segs, None, None)
        assert "a" in result and "b" in result


# ---------------------------------------------------------------------------
# Vision verdict → reasons
# ---------------------------------------------------------------------------

class TestVisionVerdictToReasons:
    def test_all_pass(self):
        from producer.review_gate import _vision_verdict_to_reasons
        reasons = _vision_verdict_to_reasons(_good_vision_verdict())
        assert all(r["pass"] for r in reasons)

    def test_animation_detected_is_auto_fail(self):
        from producer.review_gate import _vision_verdict_to_reasons
        verdict = {**_good_vision_verdict(), "animation_detected": True}
        reasons = _vision_verdict_to_reasons(verdict)
        anim_reason = next(r for r in reasons if r["check"] == "animation_detected")
        assert not anim_reason["pass"]
        assert "auto-fail" in anim_reason["reason"].lower()

    def test_hook_absent_in_mid_fail(self):
        from producer.review_gate import _vision_verdict_to_reasons
        verdict = {**_good_vision_verdict(), "hook_absent_in_mid_frame": False}
        reasons = _vision_verdict_to_reasons(verdict)
        reason = next(r for r in reasons if r["check"] == "hook_absent_in_mid_frame")
        assert not reason["pass"]

    def test_missing_key_treated_as_skip(self):
        from producer.review_gate import _vision_verdict_to_reasons
        # If the LLM didn't return a key, treat as pass (skipped)
        reasons = _vision_verdict_to_reasons({})  # empty verdict
        assert all(r["pass"] for r in reasons)


# ---------------------------------------------------------------------------
# Resolution / duration checks
# ---------------------------------------------------------------------------

class TestCheckResolutionAndDuration:
    def test_correct_resolution_passes(self):
        from producer.review_gate import _check_resolution_and_duration
        reasons = _check_resolution_and_duration(_make_probe(1080, 1920, 30.0, 30.0))
        res_reason = next(r for r in reasons if r["check"] == "resolution")
        assert res_reason["pass"]

    def test_low_resolution_fails(self):
        from producer.review_gate import _check_resolution_and_duration
        reasons = _check_resolution_and_duration(_make_probe(720, 1280, 30.0, 30.0))
        res_reason = next(r for r in reasons if r["check"] == "resolution")
        assert not res_reason["pass"]

    def test_duration_mismatch_fails(self):
        from producer.review_gate import _check_resolution_and_duration
        # Video stream says 30s but container says 45s (diff = 15 > 3s tolerance)
        reasons = _check_resolution_and_duration(_make_probe(1080, 1920, 30.0, 45.0))
        dur_reason = next(r for r in reasons if r["check"] == "duration_sanity")
        assert not dur_reason["pass"]

    def test_small_duration_diff_passes(self):
        from producer.review_gate import _check_resolution_and_duration
        # diff = 0.5s — within 3s tolerance
        reasons = _check_resolution_and_duration(_make_probe(1080, 1920, 30.0, 30.5))
        dur_reason = next(r for r in reasons if r["check"] == "duration_sanity")
        assert dur_reason["pass"]

    def test_no_video_stream_returns_fail(self):
        from producer.review_gate import _check_resolution_and_duration
        reasons = _check_resolution_and_duration({"streams": [], "format": {}})
        assert not any(r["pass"] for r in reasons)


# ---------------------------------------------------------------------------
# run_gate: meme clips always pending
# ---------------------------------------------------------------------------

class TestRunGateMemeClip:
    def test_meme_clip_returns_pending(self):
        from producer.review_gate import run_gate
        clip = _make_clip(kind="meme")
        result = run_gate(clip, "r2://campaigns/fitness/clips/1.mp4", None, _make_campaign_cfg(), None)
        assert result.gate_status == "pending"
        assert result.formula_score is None
        assert any("Meme" in r["reason"] for r in result.gate_reasons)

    def test_empty_video_path_returns_pending(self):
        from producer.review_gate import run_gate
        clip = _make_clip(kind="clip")
        result = run_gate(clip, "", None, _make_campaign_cfg(), None)
        assert result.gate_status == "pending"


# ---------------------------------------------------------------------------
# run_gate: transport errors keep gate 'pending'
# ---------------------------------------------------------------------------

class TestRunGateTransportErrors:
    def test_phase1_transport_error_returns_pending(self, monkeypatch):
        from producer import review_gate
        monkeypatch.setattr(review_gate, "_probe_video", lambda p: (_ for _ in ()).throw(RuntimeError("ffprobe not found")))
        monkeypatch.setattr(review_gate, "_extract_frames", lambda p, ts: [])
        # _resolve_to_local_path should return the path directly for non-r2
        clip = _make_clip(kind="clip")
        result = review_gate.run_gate(clip, "/tmp/fake.mp4", None, _make_campaign_cfg(), None)
        assert result.gate_status == "pending"
        assert any("gate unavailable" in r["reason"] for r in result.gate_reasons)

    def test_phase2_transport_error_returns_pending(self, monkeypatch):
        from producer import review_gate
        monkeypatch.setattr(review_gate, "_probe_video", lambda p: _make_probe())
        monkeypatch.setattr(review_gate, "_extract_frames", lambda p, ts: [b"" for _ in ts])
        monkeypatch.setattr(review_gate, "_load_style_refs", lambda name: [])
        monkeypatch.setattr(review_gate, "_vision_llm_call",
                            lambda frames, refs, dur: _good_vision_verdict())
        # Phase 2 LLM call raises
        monkeypatch.setattr(review_gate, "_content_llm_call",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("LLM unavailable")))
        clip = _make_clip(kind="clip")
        result = review_gate.run_gate(clip, "/tmp/fake.mp4", None, _make_campaign_cfg(), None)
        assert result.gate_status == "pending"
        assert any("gate unavailable" in r["reason"] for r in result.gate_reasons)


# ---------------------------------------------------------------------------
# run_gate: full happy path → ready
# ---------------------------------------------------------------------------

class TestRunGateReady:
    def test_all_checks_pass_returns_ready(self, monkeypatch):
        from producer import review_gate
        monkeypatch.setattr(review_gate, "_probe_video", lambda p: _make_probe())
        monkeypatch.setattr(review_gate, "_extract_frames", lambda p, ts: [b"" for _ in ts])
        monkeypatch.setattr(review_gate, "_load_style_refs", lambda name: [])
        monkeypatch.setattr(review_gate, "_vision_llm_call",
                            lambda frames, refs, dur: _good_vision_verdict())
        monkeypatch.setattr(review_gate, "_content_llm_call",
                            lambda *a, **kw: _good_content_verdict(0.8))

        clip = _make_clip(kind="clip")
        result = review_gate.run_gate(clip, "/tmp/fake.mp4", None, _make_campaign_cfg(), None)

        assert result.gate_status == "ready"
        assert isinstance(result.formula_score, float)
        assert abs(result.formula_score - 0.8) < 1e-6
        assert isinstance(result.gate_reasons, list)
        assert len(result.gate_reasons) > 0


# ---------------------------------------------------------------------------
# run_gate: animation auto-fail → didnt_pass
# ---------------------------------------------------------------------------

class TestRunGateAnimationFail:
    def test_animation_detected_causes_didnt_pass(self, monkeypatch):
        from producer import review_gate
        monkeypatch.setattr(review_gate, "_probe_video", lambda p: _make_probe())
        monkeypatch.setattr(review_gate, "_extract_frames", lambda p, ts: [b"" for _ in ts])
        monkeypatch.setattr(review_gate, "_load_style_refs", lambda name: [])
        anim_verdict = {**_good_vision_verdict(), "animation_detected": True, "real_humans": False}
        monkeypatch.setattr(review_gate, "_vision_llm_call",
                            lambda frames, refs, dur: anim_verdict)

        clip = _make_clip(kind="clip")
        result = review_gate.run_gate(clip, "/tmp/fake.mp4", None, _make_campaign_cfg(), None)

        assert result.gate_status == "didnt_pass"
        fail_reasons = [r for r in result.gate_reasons if not r["pass"]]
        assert any(r["check"] == "animation_detected" for r in fail_reasons)
        # Phase 2 must NOT have run (formula_score stays None)
        assert result.formula_score is None


# ---------------------------------------------------------------------------
# run_gate: low formula_score → didnt_pass
# ---------------------------------------------------------------------------

class TestRunGateLowScore:
    def test_low_score_causes_didnt_pass(self, monkeypatch):
        from producer import review_gate
        monkeypatch.setattr(review_gate, "_probe_video", lambda p: _make_probe())
        monkeypatch.setattr(review_gate, "_extract_frames", lambda p, ts: [b"" for _ in ts])
        monkeypatch.setattr(review_gate, "_load_style_refs", lambda name: [])
        monkeypatch.setattr(review_gate, "_vision_llm_call",
                            lambda frames, refs, dur: _good_vision_verdict())
        monkeypatch.setattr(review_gate, "_content_llm_call",
                            lambda *a, **kw: _good_content_verdict(0.3))

        clip = _make_clip(kind="clip")
        result = review_gate.run_gate(clip, "/tmp/fake.mp4", None, _make_campaign_cfg(), None)

        assert result.gate_status == "didnt_pass"
        assert result.formula_score is not None
        assert result.formula_score < 0.6


# ---------------------------------------------------------------------------
# run_gate: safety auto-fail → didnt_pass
# ---------------------------------------------------------------------------

class TestRunGateSafetyFail:
    def test_safety_flag_causes_didnt_pass(self, monkeypatch):
        from producer import review_gate
        monkeypatch.setattr(review_gate, "_probe_video", lambda p: _make_probe())
        monkeypatch.setattr(review_gate, "_extract_frames", lambda p, ts: [b"" for _ in ts])
        monkeypatch.setattr(review_gate, "_load_style_refs", lambda name: [])
        monkeypatch.setattr(review_gate, "_vision_llm_call",
                            lambda frames, refs, dur: _good_vision_verdict())
        verdict = _good_content_verdict(0.8)
        verdict["safety"]["unsafe_diet_content"] = True
        monkeypatch.setattr(review_gate, "_content_llm_call", lambda *a, **kw: verdict)

        clip = _make_clip(kind="clip")
        result = review_gate.run_gate(clip, "/tmp/fake.mp4", None, _make_campaign_cfg(), None)

        assert result.gate_status == "didnt_pass"
        safety_fail = next(
            r for r in result.gate_reasons
            if r["check"] == "safety_unsafe_diet_content"
        )
        assert not safety_fail["pass"]
