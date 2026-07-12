"""Tests for core/hook_style.py — strategic capitalisation enforcement."""

from __future__ import annotations

import pytest

from core.hook_style import audit_hook, enforce_hook_style, sanitize_hook


# ---------------------------------------------------------------------------
# audit_hook
# ---------------------------------------------------------------------------

class TestAuditHook:
    def test_clean_single_cap_passes(self):
        a = audit_hook("Most peptide brands NEVER show you a certificate of analysis.")
        assert a["violations"] == []
        assert a["cap_count"] == 1
        assert a["caps"] == ["NEVER"]

    def test_em_dash_flagged(self):
        a = audit_hook("Peptides changed everything — nobody talks about it")
        assert "em_dash_present" in a["violations"]

    def test_en_dash_flagged(self):
        a = audit_hook("Peptides – the real story")
        assert "em_dash_present" in a["violations"]

    def test_all_caps_line_flagged(self):
        a = audit_hook("THIS CHANGES EVERYTHING FOREVER")
        assert "all_caps_line" in a["violations"]

    def test_adjacent_caps_flagged(self):
        a = audit_hook("You should STEAL THIS system today my friend")
        assert "adjacent_caps" in a["violations"]

    def test_too_many_caps_flagged(self):
        a = audit_hook("STOP and STEAL this WILD system")
        assert "too_many_caps" in a["violations"]

    def test_three_caps_allowed_over_ten_words(self):
        hook = "He BROKE every rule they wrote and then he made MILLIONS by staying QUIET"
        a = audit_hook(hook)
        assert a["word_count"] > 10
        assert "too_many_caps" not in a["violations"]

    def test_cap_ratio_two_caps_in_six_words(self):
        a = audit_hook("STEAL this and BANK the money")
        assert "cap_ratio_exceeded" in a["violations"]

    def test_single_cap_short_hook_no_ratio_violation(self):
        # One cap is always allowed even in a short hook (contrarian opener).
        a = audit_hook("STOP doing this")
        assert "cap_ratio_exceeded" not in a["violations"]

    def test_connective_cap_flagged(self):
        a = audit_hook("This is THE moment everything changed")
        assert "connective_capitalised" in a["violations"]

    def test_acronyms_do_not_count(self):
        a = audit_hook("The FDA quietly banned the most effective peptides")
        assert a["cap_count"] == 0
        assert a["violations"] == []

    def test_digit_tokens_do_not_count(self):
        a = audit_hook("BPC-157 and GLP-1 changed his recovery NEVER seen before")
        assert a["caps"] == ["NEVER"]

    def test_protected_tokens_extension(self):
        a = audit_hook("VICI made this clip", protected_tokens=("VICI",))
        assert a["cap_count"] == 0

    def test_empty_hook(self):
        a = audit_hook("")
        assert a["cap_count"] == 0
        assert a["violations"] == []


# ---------------------------------------------------------------------------
# sanitize_hook
# ---------------------------------------------------------------------------

class TestSanitizeHook:
    def test_spaced_em_dash_becomes_full_stop(self):
        out = sanitize_hook("The FDA banned peptides overnight — RFK called it illegal")
        assert "—" not in out
        assert "banned peptides overnight. RFK" in out

    def test_tight_em_dash_becomes_comma(self):
        out = sanitize_hook("peptides—the real story")
        assert "—" not in out
        assert "peptides, the real story" in out

    def test_capitalises_after_introduced_full_stop(self):
        out = sanitize_hook("he lost 100 lbs — a peptide helped him")
        assert ". A peptide" in out

    def test_all_caps_line_demoted_to_sentence_case(self):
        out = sanitize_hook("THIS CHANGES EVERYTHING FOREVER")
        assert out == "This changes everything forever"

    def test_all_caps_preserves_acronyms(self):
        out = sanitize_hook("THE FDA BANNED EVERYTHING")
        assert "FDA" in out
        assert audit_hook(out)["violations"] == []

    def test_adjacent_caps_keeps_first(self):
        out = sanitize_hook("You should STEAL THIS system right now honestly")
        a = audit_hook(out)
        assert not a["adjacent_caps"]
        assert "STEAL" in out.split()
        assert "THIS" not in out.split()

    def test_cap_budget_keeps_first_and_last(self):
        out = sanitize_hook("STOP and STEAL this WILD system before they DELETE it all forever")
        a = audit_hook(out)
        assert a["cap_count"] <= 3
        assert "too_many_caps" not in a["violations"]

    def test_ratio_guard_drops_to_one_cap(self):
        out = sanitize_hook("STEAL this and BANK the money")
        a = audit_hook(out)
        assert a["cap_count"] == 1
        assert a["violations"] == []

    def test_ratio_guard_prefers_non_first_word(self):
        out = sanitize_hook("STEAL this and BANK the money")
        # First-word cap is the weaker position (rule 6.1) — keep BANK.
        assert "BANK" in out.split()

    def test_connective_demoted(self):
        out = sanitize_hook("This is THE moment everything changed")
        assert "THE" not in out.split()
        assert audit_hook(out)["violations"] == []

    def test_clean_hook_unchanged(self):
        hook = "Most peptide brands NEVER show you a certificate of analysis."
        assert sanitize_hook(hook) == hook

    def test_spec_worked_example_not_random(self):
        hook = "Growing on TikTok is NOT random."
        assert sanitize_hook(hook) == hook

    def test_sentence_start_demotion_keeps_capital(self):
        out = sanitize_hook("NEVER EVER post this")
        words = out.split()
        # NEVER kept (first of adjacent run), EVER demoted to lowercase
        assert words[0] == "NEVER"
        assert words[1] == "ever"

    def test_idempotent(self):
        hook = "The FDA banned peptides overnight — RFK called it ILLEGAL and WRONG today"
        once = sanitize_hook(hook)
        twice = sanitize_hook(once)
        assert once == twice
        assert audit_hook(twice)["violations"] == []

    def test_empty_hook_passthrough(self):
        assert sanitize_hook("") == ""
        assert sanitize_hook("   ") == "   "

    def test_real_peptide_hook_with_em_dash(self):
        # The exact style of hook the peptides campaign produced (2026-07-10):
        hook = "The FDA banned effective peptides overnight in 2023 — RFK called it illegal"
        out = sanitize_hook(hook)
        assert "—" not in out
        assert "FDA" in out and "RFK" in out
        assert audit_hook(out)["violations"] == []


# ---------------------------------------------------------------------------
# enforce_hook_style + llm wiring
# ---------------------------------------------------------------------------

class TestEnforcement:
    def test_enforce_wrapper(self):
        assert enforce_hook_style("a — b") == "a, b" or "—" not in enforce_hook_style("a — b")

    def test_validate_moments_applies_style(self):
        from core.llm import _validate_moments

        raw = [{
            "start": 0.0,
            "end": 30.0,
            "score": 0.8,
            "hook": "This peptide changed everything — STEAL THIS protocol",
            "reason": "strong",
        }]
        out = _validate_moments(raw, (10, 60))
        assert len(out) == 1
        hook = out[0]["hook"]
        assert "—" not in hook
        assert not audit_hook(hook)["adjacent_caps"]

    def test_prompt_contains_hook_style_rules(self):
        from core.llm import _build_prompt

        p = _build_prompt(
            [{"start": 0.0, "end": 5.0, "text": "hello world"}],
            "rules", None, (10, 60), 5,
        )
        assert "HOOK STYLE RULES" in p
        assert "em dash" in p
