"""
tests/test_hook_body_gate.py — Tests for hook/body match (Req A1) and
topical relevance (Req A3) gates in producer/review_gate.py.

All LLM calls are mocked — no network calls.

Covers:
  hook_body_match:
    - matches=true  → pass (reason added, no hard fail)
    - matches=false → HARD didnt_pass (clip-76-style mismatch)
    - absent field  → back-compat PASS (no reason entry added)
    - not relaxable via relaxed_safety_checks

  topical_relevance:
    - on_topic=true  → pass
    - on_topic=false → HARD didnt_pass (clip-87 generic-advice case)
    - absent field   → back-compat PASS
    - not relaxable via relaxed_safety_checks

  _run_phase2 integration:
    - hook_body mismatch → gate_status='didnt_pass' even at high formula_score
    - topical fail       → gate_status='didnt_pass' even at high formula_score
    - both absent        → gate_status='ready' (no regression on existing behaviour)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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


def _make_clip(id=1, kind="clip", campaign="fitness", hook="test hook",
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


def _make_campaign_cfg(ranking_rules="Be informative and engaging."):
    cfg = MagicMock()
    cfg.ranking.ranking_rules = ranking_rules
    cfg.ranking.stance = ""
    cfg.gate.relaxed_safety_checks = []
    return cfg


def _base_verdict(
    hook_body_match: dict | None = None,
    topical_relevance: dict | None = None,
    scores: dict | None = None,
    safety: dict | None = None,
) -> dict:
    """Build a verdict dict; omit new fields when not provided (absent = pass)."""
    v: dict = {
        "scores": scores if scores is not None else _GOOD_SCORES.copy(),
        "safety": safety if safety is not None else _SAFE_FLAGS.copy(),
    }
    if hook_body_match is not None:
        v["hook_body_match"] = hook_body_match
    if topical_relevance is not None:
        v["topical_relevance"] = topical_relevance
    return v


# ---------------------------------------------------------------------------
# _score_content_verdict — hook_body_match field
# ---------------------------------------------------------------------------

class TestHookBodyMatchField:

    def test_matches_true_passes(self):
        """hook_body_match.matches=true → reason added, check passes."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(
            hook_body_match={"matches": True, "reason": "body covers CJC-1295"},
        )
        formula_score, reasons = _score_content_verdict(verdict)

        hbm = [r for r in reasons if r["check"] == "hook_body_match"]
        assert len(hbm) == 1
        assert hbm[0]["pass"] is True
        assert "OK" in hbm[0]["reason"]

    def test_matches_false_hard_fail(self):
        """hook_body_match.matches=false → HARD fail (clip-76 CJC/retatrutide case)."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(
            hook_body_match={
                "matches": False,
                "reason": (
                    "hook says CJC-1295 secretagogues but body is "
                    "entirely retatrutide side effects and dosing"
                ),
            },
        )
        formula_score, reasons = _score_content_verdict(verdict)

        hbm = [r for r in reasons if r["check"] == "hook_body_match"]
        assert len(hbm) == 1
        assert hbm[0]["pass"] is False
        assert "HOOK/BODY MISMATCH" in hbm[0]["reason"]

        # The threshold check should also reflect the combined fail
        threshold = next(r for r in reasons if r["check"] == "formula_score_threshold")
        assert threshold["pass"] is False

    def test_absent_hook_body_match_is_pass(self):
        """No hook_body_match field → back-compat: no reason entry, no fail."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict()  # no hook_body_match
        formula_score, reasons = _score_content_verdict(verdict)

        hbm = [r for r in reasons if r["check"] == "hook_body_match"]
        assert len(hbm) == 0  # absent field adds no reason

    def test_null_matches_does_not_hard_fail(self):
        """matches=null (truncated/null-defaulting LLM output) → PASS, not a
        spurious hard fail (reviewer issue #1)."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(hook_body_match={"matches": None, "reason": ""})
        formula_score, reasons = _score_content_verdict(verdict)

        hbm = next(r for r in reasons if r["check"] == "hook_body_match")
        assert hbm["pass"] is True
        threshold = next(r for r in reasons if r["check"] == "formula_score_threshold")
        assert threshold["pass"] is True

    def test_null_on_topic_does_not_hard_fail(self):
        """topical_relevance.on_topic=null → PASS (reviewer issue #1)."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(topical_relevance={"on_topic": None, "reason": ""})
        _, reasons = _score_content_verdict(verdict)

        tr = next(r for r in reasons if r["check"] == "topical_relevance")
        assert tr["pass"] is True

    def test_hook_body_fail_not_relaxable(self):
        """hook_body_match=false fails EVEN WHEN relaxed_safety_checks is set."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(
            hook_body_match={"matches": False, "reason": "mismatch"},
            safety={"medical_claims": True, "unsafe_diet_content": False,
                    "harmful_content": False, "guideline_violation": False},
        )
        # Relaxing medical_claims should NOT rescue the hook_body fail
        formula_score, reasons = _score_content_verdict(
            verdict, relaxed_safety_checks=["medical_claims"]
        )
        hbm = [r for r in reasons if r["check"] == "hook_body_match"]
        assert hbm[0]["pass"] is False
        threshold = next(r for r in reasons if r["check"] == "formula_score_threshold")
        assert threshold["pass"] is False

    def test_matches_false_clips_clip76_style(self):
        """Exact clip-76 scenario: CJC hook, retatrutide body → HARD fail."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(
            scores={k: 0.75 for k in _GOOD_SCORES},  # high scores otherwise
            hook_body_match={
                "matches": False,
                "reason": (
                    "Hook promises GH secretagogues/CJC-1295 but transcript "
                    "covers retatrutide allodynia, pancreatitis, gallstones, "
                    "and dosing — completely different subject."
                ),
            },
        )
        formula_score, reasons = _score_content_verdict(verdict)
        hbm = [r for r in reasons if r["check"] == "hook_body_match"]
        assert hbm[0]["pass"] is False
        assert formula_score >= 0.6  # score itself is fine; the hard fail overrides

    def test_matches_false_reason_default_when_empty(self):
        """When matches=false but no reason text, a default reason is used."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(
            hook_body_match={"matches": False, "reason": ""},
        )
        _, reasons = _score_content_verdict(verdict)
        hbm = next(r for r in reasons if r["check"] == "hook_body_match")
        assert "HOOK/BODY MISMATCH" in hbm["reason"]
        assert len(hbm["reason"]) > len("HOOK/BODY MISMATCH: ")  # has a default message


# ---------------------------------------------------------------------------
# _score_content_verdict — topical_relevance field
# ---------------------------------------------------------------------------

class TestTopicalRelevanceField:

    def test_on_topic_true_passes(self):
        """topical_relevance.on_topic=true → reason added, check passes."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(
            topical_relevance={"on_topic": True, "reason": "substantively covers BPC-157"},
        )
        formula_score, reasons = _score_content_verdict(verdict)

        tr = [r for r in reasons if r["check"] == "topical_relevance"]
        assert len(tr) == 1
        assert tr[0]["pass"] is True
        assert "OK" in tr[0]["reason"]

    def test_on_topic_false_hard_fail(self):
        """topical_relevance.on_topic=false → HARD fail (clip-87 generic-advice case)."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(
            topical_relevance={
                "on_topic": False,
                "reason": (
                    "body is generic hydration/magnesium/medical-disclaimer content "
                    "with one passing retatrutide mention at the end"
                ),
            },
        )
        formula_score, reasons = _score_content_verdict(verdict)

        tr = [r for r in reasons if r["check"] == "topical_relevance"]
        assert len(tr) == 1
        assert tr[0]["pass"] is False
        assert "TOPICAL RELEVANCE FAIL" in tr[0]["reason"]

        threshold = next(r for r in reasons if r["check"] == "formula_score_threshold")
        assert threshold["pass"] is False

    def test_absent_topical_relevance_is_pass(self):
        """No topical_relevance field → back-compat: no reason entry, no fail."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict()  # no topical_relevance
        formula_score, reasons = _score_content_verdict(verdict)

        tr = [r for r in reasons if r["check"] == "topical_relevance"]
        assert len(tr) == 0

    def test_topical_fail_not_relaxable(self):
        """topical_relevance=false fails EVEN WHEN relaxed_safety_checks is set."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(
            topical_relevance={"on_topic": False, "reason": "generic advice"},
            safety={"medical_claims": True, "unsafe_diet_content": False,
                    "harmful_content": False, "guideline_violation": False},
        )
        formula_score, reasons = _score_content_verdict(
            verdict, relaxed_safety_checks=["medical_claims"]
        )
        tr = next(r for r in reasons if r["check"] == "topical_relevance")
        assert tr["pass"] is False
        threshold = next(r for r in reasons if r["check"] == "formula_score_threshold")
        assert threshold["pass"] is False

    def test_generic_advice_clip87_style(self):
        """Exact clip-87 scenario: 'racing heart on peptides' hook, generic body."""
        from producer.review_gate import _score_content_verdict

        verdict = _base_verdict(
            scores={k: 0.7 for k in _GOOD_SCORES},  # decent scores otherwise
            topical_relevance={
                "on_topic": False,
                "reason": (
                    "Body is hydration/magnesium/medical-disclaimer advice with "
                    "one passing peptide mention — not substantively about peptides."
                ),
            },
        )
        formula_score, reasons = _score_content_verdict(verdict)
        tr = next(r for r in reasons if r["check"] == "topical_relevance")
        assert tr["pass"] is False
        assert formula_score >= 0.6  # score is fine; hard fail overrides


# ---------------------------------------------------------------------------
# _run_phase2 integration — gate_status determination
# ---------------------------------------------------------------------------

class TestRunPhase2HookBodyAndTopical:

    def _patch_content_call(self, monkeypatch, verdict: dict):
        import producer.review_gate as rg
        monkeypatch.setattr(rg, "_content_llm_call", lambda *a, **kw: verdict)

    def test_hook_body_mismatch_causes_didnt_pass(self, monkeypatch):
        """hook_body_match.matches=false → gate_status='didnt_pass'."""
        import producer.review_gate as rg
        from producer.review_gate import _run_phase2

        verdict = _base_verdict(
            hook_body_match={
                "matches": False,
                "reason": "hook is about CJC-1295; body is retatrutide",
            },
        )
        self._patch_content_call(monkeypatch, verdict)

        clip = _make_clip(hook="GH secretagogues like CJC-1295")
        campaign_cfg = _make_campaign_cfg()
        gate_status, fs, reasons = _run_phase2(clip, [], campaign_cfg, [])
        assert gate_status == "didnt_pass"
        fail_reasons = [r for r in reasons if r["check"] == "hook_body_match" and not r["pass"]]
        assert len(fail_reasons) == 1

    def test_topical_relevance_fail_causes_didnt_pass(self, monkeypatch):
        """topical_relevance.on_topic=false → gate_status='didnt_pass'."""
        import producer.review_gate as rg
        from producer.review_gate import _run_phase2

        verdict = _base_verdict(
            topical_relevance={
                "on_topic": False,
                "reason": "body is generic advice",
            },
        )
        self._patch_content_call(monkeypatch, verdict)

        clip = _make_clip(hook="racing heart on peptides")
        campaign_cfg = _make_campaign_cfg()
        gate_status, fs, reasons = _run_phase2(clip, [], campaign_cfg, [])
        assert gate_status == "didnt_pass"
        fail_reasons = [r for r in reasons if r["check"] == "topical_relevance" and not r["pass"]]
        assert len(fail_reasons) == 1

    def test_both_absent_no_regression(self, monkeypatch):
        """When neither new field is present, existing clips still reach 'ready'."""
        import producer.review_gate as rg
        from producer.review_gate import _run_phase2

        verdict = _base_verdict()  # no hook_body_match, no topical_relevance
        self._patch_content_call(monkeypatch, verdict)

        clip = _make_clip()
        campaign_cfg = _make_campaign_cfg()
        gate_status, fs, reasons = _run_phase2(clip, [], campaign_cfg, [])
        assert gate_status == "ready"

    def test_high_score_does_not_rescue_hook_body_fail(self, monkeypatch):
        """formula_score=0.9 does NOT rescue a hook_body_match=false."""
        import producer.review_gate as rg
        from producer.review_gate import _run_phase2

        verdict = _base_verdict(
            scores={k: 0.9 for k in _GOOD_SCORES},
            hook_body_match={"matches": False, "reason": "mismatch"},
        )
        self._patch_content_call(monkeypatch, verdict)

        clip = _make_clip()
        campaign_cfg = _make_campaign_cfg()
        gate_status, fs, reasons = _run_phase2(clip, [], campaign_cfg, [])
        assert gate_status == "didnt_pass"
        assert fs is not None and fs >= 0.6

    def test_high_score_does_not_rescue_topical_fail(self, monkeypatch):
        """formula_score=0.9 does NOT rescue a topical_relevance.on_topic=false."""
        import producer.review_gate as rg
        from producer.review_gate import _run_phase2

        verdict = _base_verdict(
            scores={k: 0.9 for k in _GOOD_SCORES},
            topical_relevance={"on_topic": False, "reason": "generic"},
        )
        self._patch_content_call(monkeypatch, verdict)

        clip = _make_clip()
        campaign_cfg = _make_campaign_cfg()
        gate_status, fs, reasons = _run_phase2(clip, [], campaign_cfg, [])
        assert gate_status == "didnt_pass"
        assert fs is not None and fs >= 0.6
