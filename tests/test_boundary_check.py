"""
tests/test_boundary_check.py — Unit tests for producer/boundary_check.py.

All LLM calls are mocked via monkeypatch.  No network calls.
Covers:
- is_bad_start_sentence: continuation openers, question sentences
- needs_end_extension: dangling comparisons
- apply_prefilters: start bump, end extension, max-duration enforcement
- verify_boundaries: pass / fail / adjust / transport-error paths
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

def _spans(*texts_with_times: tuple[str, float, float]) -> list[dict]:
    return [{"text": t, "start": s, "end": e} for t, s, e in texts_with_times]


def _candidate(start: float, end: float, score: float = 0.8) -> dict:
    return {"start": start, "end": end, "score": score, "hook": "Test hook", "reason": ""}


# ---------------------------------------------------------------------------
# is_bad_start_sentence
# ---------------------------------------------------------------------------

class TestIsBadStartSentence:
    def test_continuation_so(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("So we were talking about peptides.") is True

    def test_continuation_and(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("And the results were incredible.") is True

    def test_continuation_but(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("But that's not the whole story.") is True

    def test_continuation_well(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("Well, it depends on your goals.") is True

    def test_continuation_yeah(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("Yeah, exactly what I was thinking.") is True

    def test_continuation_right(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("Right, so that's the key insight.") is True

    def test_continuation_exactly(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("Exactly, that's what makes peptides powerful.") is True

    def test_continuation_totally(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("Totally agree with that assessment.") is True

    def test_continuation_i_mean(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("I mean, that's the whole point.") is True

    def test_question_as_clip_start(self):
        from producer.boundary_check import is_bad_start_sentence
        # A sentence ending in "?" is an interviewer question — bad start
        assert is_bad_start_sentence("What do peptides actually do?") is True

    def test_clean_statement_ok(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("BPC-157 accelerates tendon healing.") is False

    def test_case_insensitive_so(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("SO this is what happened.") is True

    def test_word_boundary_sociology(self):
        from producer.boundary_check import is_bad_start_sentence
        # "Sociology" starts with "so" but must NOT trigger the rule (word boundary)
        assert is_bad_start_sentence("Sociology is fascinating.") is False

    def test_empty_string_ok(self):
        from producer.boundary_check import is_bad_start_sentence
        assert is_bad_start_sentence("") is False

    def test_prev_text_question_does_not_block_answer(self):
        from producer.boundary_check import is_bad_start_sentence
        # Answering a previous question is fine — prev_text ending in "?" is NOT a blocker
        assert is_bad_start_sentence("BPC-157 helps with tendon repair.", "What is BPC-157?") is False


# ---------------------------------------------------------------------------
# needs_end_extension
# ---------------------------------------------------------------------------

class TestNeedsEndExtension:
    def test_ends_with_like(self):
        from producer.boundary_check import needs_end_extension
        assert needs_end_extension("It feels like") is True

    def test_ends_with_like_punctuation(self):
        from producer.boundary_check import needs_end_extension
        assert needs_end_extension("It works like.") is True

    def test_ends_with_than(self):
        from producer.boundary_check import needs_end_extension
        assert needs_end_extension("It's better than") is True

    def test_ends_with_as_if(self):
        from producer.boundary_check import needs_end_extension
        assert needs_end_extension("It acts as if") is True

    def test_clean_ending_ok(self):
        from producer.boundary_check import needs_end_extension
        assert needs_end_extension("The peptide heals the tendon.") is False

    def test_empty_string_ok(self):
        from producer.boundary_check import needs_end_extension
        assert needs_end_extension("") is False

    def test_like_in_middle_not_flagged(self):
        from producer.boundary_check import needs_end_extension
        # "like" in the middle — clean ending after it
        assert needs_end_extension("I like this peptide very much.") is False


# ---------------------------------------------------------------------------
# apply_prefilters
# ---------------------------------------------------------------------------

class TestApplyPrefilters:
    def test_clean_clip_unchanged(self):
        """A clip with clean start and end is not modified."""
        from producer.boundary_check import apply_prefilters

        spans = _spans(
            ("BPC-157 heals tendons rapidly.", 0.0, 5.0),
            ("Research shows 80% improvement.", 5.0, 10.0),
            ("This is the final sentence.", 10.0, 15.0),
        )
        c = _candidate(0.0, 10.0)
        result = apply_prefilters(c, spans, (5, 60))
        assert result["start"] == 0.0
        assert result["end"] == 10.0

    def test_bad_start_bumped_forward(self):
        """A clip starting on a continuation sentence is bumped forward."""
        from producer.boundary_check import apply_prefilters

        spans = _spans(
            ("So we were talking about healing.", 0.0, 5.0),
            ("BPC-157 accelerates this process.", 5.0, 10.0),
            ("Studies confirm the effect.", 10.0, 15.0),
        )
        # Clip starts at 0 (continuation "So ...") — should bump to index 1
        c = _candidate(0.0, 15.0)
        result = apply_prefilters(c, spans, (5, 60))
        assert result["start"] == 5.0, f"Expected bumped start; got {result['start']}"

    def test_question_start_bumped_forward(self):
        """Clip starting on interviewer question is bumped forward."""
        from producer.boundary_check import apply_prefilters

        spans = _spans(
            ("What do peptides do to the body?", 0.0, 4.0),
            ("They signal tissue repair pathways.", 4.0, 9.0),
            ("The results are remarkable.", 9.0, 14.0),
        )
        c = _candidate(0.0, 14.0)
        result = apply_prefilters(c, spans, (5, 60))
        assert result["start"] == 4.0

    def test_max_two_start_bumps(self):
        """Only up to 2 start bumps are applied."""
        from producer.boundary_check import apply_prefilters

        spans = _spans(
            ("So first issue.", 0.0, 3.0),
            ("And second issue.", 3.0, 6.0),
            ("But third issue.", 6.0, 9.0),
            ("The real content starts here.", 9.0, 15.0),
        )
        c = _candidate(0.0, 15.0)
        result = apply_prefilters(c, spans, (5, 60))
        # 2 bumps max: from 0→1→2 (stopped at 2 bad ones)
        assert result["start"] in (3.0, 6.0), f"Expected 2 bumps max; got start={result['start']}"

    def test_dangling_end_extended(self):
        """Clip ending with 'like' is extended by one sentence."""
        from producer.boundary_check import apply_prefilters

        spans = _spans(
            ("BPC-157 works like", 0.0, 4.0),
            ("a signal to your body to heal.", 4.0, 9.0),
        )
        c = _candidate(0.0, 4.0)
        result = apply_prefilters(c, spans, (3, 60))
        assert result["end"] == 9.0

    def test_max_duration_enforced_after_extension(self):
        """Extension does not push clip beyond clip_len max."""
        from producer.boundary_check import apply_prefilters

        spans = _spans(
            ("Sentence one.", 0.0, 5.0),
            ("Sentence two.", 5.0, 10.0),
            ("Sentence three.", 10.0, 15.0),
            ("Extension like", 15.0, 20.0),
            ("Fifth sentence.", 20.0, 25.0),
        )
        c = _candidate(0.0, 20.0)
        result = apply_prefilters(c, spans, (5, 15))  # max 15s
        duration = result["end"] - result["start"]
        assert duration <= 15.0

    def test_no_spans_returns_original(self):
        """When sentence_spans is empty, candidate is returned unchanged."""
        from producer.boundary_check import apply_prefilters

        c = _candidate(5.0, 25.0)
        result = apply_prefilters(c, [], (5, 60))
        assert result["start"] == 5.0
        assert result["end"] == 25.0


# ---------------------------------------------------------------------------
# verify_boundaries — pass / fail / adjust / transport-error paths
# ---------------------------------------------------------------------------

def _make_llm_mock(response_text: str):
    """Return a mock LLM call that returns the given text."""
    msg = MagicMock()
    msg.content = [MagicMock(type="text", text=response_text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    return client, msg


class TestVerifyBoundaries:
    def _patch_client(self, monkeypatch, verdict_json: str, second_verdict_json: str | None = None):
        """Patch _get_boundary_client to return a mock that yields verdict_json."""
        import json as _json
        call_count = [0]

        def fake_create_completion(client, model, max_tokens, messages):
            call_count[0] += 1
            text = verdict_json if (second_verdict_json is None or call_count[0] == 1) else second_verdict_json
            msg = MagicMock()
            msg.content = [MagicMock(type="text", text=text)]
            return msg

        import producer.boundary_check as bc_mod
        mock_client = MagicMock()
        monkeypatch.setattr(bc_mod, "_get_boundary_client", lambda: (mock_client, "test-model"))

        import core.llm as llm_mod
        monkeypatch.setattr(llm_mod, "create_completion", fake_create_completion)
        monkeypatch.setattr(llm_mod, "extract_text", lambda msg: msg.content[0].text)

    def test_pass_verdict_returns_original_keep_true(self, monkeypatch):
        import producer.boundary_check as bc_mod
        self._patch_client(
            monkeypatch,
            '{"verdict":"pass","reason":"looks good","adjusted_start_sentences":0,"adjusted_end_sentences":0}',
        )
        spans = _spans(
            ("BPC-157 heals tendons.", 0.0, 5.0),
            ("Studies confirm this.", 5.0, 10.0),
            ("The effect is remarkable.", 10.0, 15.0),
        )
        c = _candidate(0.0, 10.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (5, 60))
        assert keep is True

    def test_fail_no_adjust_drop(self, monkeypatch):
        """Fail on both calls → should_keep=False."""
        import producer.boundary_check as bc_mod
        fail_json = '{"verdict":"fail","reason":"bad boundary","adjusted_start_sentences":0,"adjusted_end_sentences":0}'
        self._patch_client(monkeypatch, fail_json, fail_json)

        spans = _spans(
            ("So this continues.", 0.0, 5.0),
            ("Bad ending like", 5.0, 10.0),
        )
        c = _candidate(0.0, 10.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (3, 60))
        assert keep is False

    def test_fail_with_adjust_then_pass(self, monkeypatch):
        """First call fails with adjustment; second call passes → keep=True."""
        import producer.boundary_check as bc_mod
        fail_json = '{"verdict":"fail","reason":"bad start","adjusted_start_sentences":1,"adjusted_end_sentences":0}'
        pass_json = '{"verdict":"pass","reason":"good now","adjusted_start_sentences":0,"adjusted_end_sentences":0}'
        self._patch_client(monkeypatch, fail_json, pass_json)

        spans = _spans(
            ("So this is a bad start.", 0.0, 5.0),
            ("BPC-157 heals tendons.", 5.0, 10.0),
            ("Studies confirm this.", 10.0, 15.0),
        )
        c = _candidate(0.0, 15.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (5, 60))
        assert keep is True
        assert adjusted["start"] >= 5.0  # start was bumped

    def test_transport_error_returns_pass(self, monkeypatch):
        """LLM transport error → treat as pass, never block pipeline."""
        import producer.boundary_check as bc_mod

        def raise_err():
            raise ConnectionError("network unavailable")

        monkeypatch.setattr(bc_mod, "_get_boundary_client", raise_err)

        spans = _spans(
            ("BPC-157 heals tendons.", 0.0, 5.0),
            ("Studies confirm this.", 5.0, 10.0),
        )
        c = _candidate(0.0, 10.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (5, 60))
        assert keep is True

    def test_no_spans_returns_keep_true(self):
        """No sentence spans → always keep (cannot verify)."""
        from producer.boundary_check import verify_boundaries
        c = _candidate(0.0, 30.0)
        adjusted, keep = verify_boundaries(c, [], (5, 60))
        assert keep is True

    def test_parse_error_in_response_treated_as_pass(self, monkeypatch):
        """Unparseable LLM response → default to pass verdict."""
        import producer.boundary_check as bc_mod
        self._patch_client(monkeypatch, "Not JSON at all")

        spans = _spans(
            ("BPC-157 heals tendons.", 0.0, 5.0),
            ("Studies confirm this.", 5.0, 10.0),
        )
        c = _candidate(0.0, 10.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (5, 60))
        assert keep is True

    def test_fail_with_adjust_then_pass_updated(self, monkeypatch):
        """Under repair-first logic: fail+adj≠0 → REPAIR immediately (one call).

        The second LLM call is never made — the adjustment is applied and the
        clip is kept without re-verifying.  This is the core change from the old
        double-verify design.
        """
        import producer.boundary_check as bc_mod
        call_count = [0]

        def fake_create_completion(client, model, max_tokens, messages):
            call_count[0] += 1
            if call_count[0] > 1:
                raise AssertionError("Second LLM call must not happen for fail+adj≠0")
            msg = MagicMock()
            msg.content = [MagicMock(
                type="text",
                text='{"verdict":"fail","reason":"bad start","adjusted_start_sentences":1,"adjusted_end_sentences":0}',
            )]
            return msg

        import core.llm as llm_mod
        mock_client = MagicMock()
        monkeypatch.setattr(bc_mod, "_get_boundary_client", lambda: (mock_client, "test-model"))
        monkeypatch.setattr(llm_mod, "create_completion", fake_create_completion)
        monkeypatch.setattr(llm_mod, "extract_text", lambda msg: msg.content[0].text)

        spans = _spans(
            ("So this is a bad start.", 0.0, 5.0),
            ("BPC-157 heals tendons.", 5.0, 10.0),
            ("Studies confirm this.", 10.0, 15.0),
        )
        c = _candidate(0.0, 15.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (5, 60))
        assert keep is True
        assert adjusted["start"] >= 5.0   # delta_start=1 bumped start forward
        assert call_count[0] == 1         # exactly one LLM call


class TestVerifyBoundariesRepair:
    """New tests for the repair-first (adjust-first, drop-last) logic."""

    def _patch_client(
        self,
        monkeypatch,
        first_verdict: str,
        second_verdict: str | None = None,
    ):
        """Patch LLM client; optional second_verdict for the start-shift round."""
        call_count = [0]

        def fake_create_completion(client, model, max_tokens, messages):
            call_count[0] += 1
            text = (
                first_verdict
                if (second_verdict is None or call_count[0] == 1)
                else second_verdict
            )
            msg = MagicMock()
            msg.content = [MagicMock(type="text", text=text)]
            return msg

        import producer.boundary_check as bc_mod
        import core.llm as llm_mod
        mock_client = MagicMock()
        monkeypatch.setattr(bc_mod, "_get_boundary_client", lambda: (mock_client, "test-model"))
        monkeypatch.setattr(llm_mod, "create_completion", fake_create_completion)
        monkeypatch.setattr(llm_mod, "extract_text", lambda msg: msg.content[0].text)
        return call_count

    # ── Repair via end trim ────────────────────────────────────────────────────

    def test_fail_end_trim_adj_keeps_clip(self, monkeypatch):
        """fail + adjusted_end_sentences=-1 → trim last sentence, keep clip.

        This is the clip-80 / list-item-bleed pattern.  Under repair-first logic
        the adjusted clip is kept without a second LLM call.
        """
        import producer.boundary_check as bc_mod
        fail_adj = (
            '{"verdict":"fail","reason":"last sentence starts new list item",'
            '"adjusted_start_sentences":0,"adjusted_end_sentences":-1}'
        )
        call_count = self._patch_client(monkeypatch, fail_adj)

        spans = _spans(
            ("BPC-157 heals tendons.", 0.0, 10.0),
            ("Studies confirm 80% improvement.", 10.0, 20.0),
            ("Number 16, CAX. This is like Adderall.", 20.0, 30.0),
        )
        c = _candidate(0.0, 30.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (10, 60))

        assert keep is True
        assert adjusted["end"] == 20.0    # last sentence trimmed
        assert call_count[0] == 1         # exactly one LLM call — no re-verify

    def test_fail_start_adj_keeps_clip(self, monkeypatch):
        """fail + adjusted_start_sentences=+1 → skip bad opening sentence, keep."""
        import producer.boundary_check as bc_mod
        fail_adj = (
            '{"verdict":"fail","reason":"starts with continuation opener",'
            '"adjusted_start_sentences":1,"adjusted_end_sentences":0}'
        )
        call_count = self._patch_client(monkeypatch, fail_adj)

        spans = _spans(
            ("So anyway, here we are.", 0.0, 8.0),
            ("BPC-157 is the topic.", 8.0, 18.0),
            ("Studies confirm the effect.", 18.0, 28.0),
        )
        c = _candidate(0.0, 28.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (10, 60))

        assert keep is True
        assert adjusted["start"] == 8.0   # first sentence skipped
        assert call_count[0] == 1

    def test_fail_end_trim_min_len_prevents_trim_still_keeps(self, monkeypatch):
        """fail + adj_end=-1 but min_len prevents the trim → KEEP (one call).

        _apply_boundary_deltas enforces min_len by re-extending the end, so the
        adjusted candidate may end up the same as the original.  We still KEEP
        because the verifier provided a non-zero adjustment (it tried to fix the
        boundary).  The review critic downstream gates final quality.
        """
        import producer.boundary_check as bc_mod
        fail_adj = (
            '{"verdict":"fail","reason":"trailing list item",'
            '"adjusted_start_sentences":0,"adjusted_end_sentences":-1}'
        )
        call_count = self._patch_client(monkeypatch, fail_adj)

        # 2 sentences of 5s each; trimming last would give 5s < min 8s.
        # _apply_boundary_deltas extends back to include the trailing sentence
        # to satisfy min_len, so adjusted.end == 10.0 (unchanged).
        spans = _spans(
            ("Good content here.", 0.0, 5.0),
            ("Moving on to the next.", 5.0, 10.0),
        )
        c = _candidate(0.0, 10.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (8, 60))

        assert keep is True                # KEEP even when trim can't fully apply
        assert adjusted["end"] == 10.0     # min_len enforcement kept original end
        assert call_count[0] == 1          # exactly one LLM call

    def test_fail_both_adj_nonzero_single_call(self, monkeypatch):
        """fail + adj_start=+1, adj_end=-1 → both applied, single call, keep."""
        import producer.boundary_check as bc_mod
        fail_adj = (
            '{"verdict":"fail","reason":"bad start and trailing bleed",'
            '"adjusted_start_sentences":1,"adjusted_end_sentences":-1}'
        )
        call_count = self._patch_client(monkeypatch, fail_adj)

        # 5 sentences of 10s each = 50s clip; trim 1 from each end → 30s
        spans = _spans(
            ("So we continue from before.", 0.0, 10.0),
            ("The real point is here.", 10.0, 20.0),
            ("More detail about this.", 20.0, 30.0),
            ("That wraps up the idea.", 30.0, 40.0),
            ("And just like that, next topic.", 40.0, 50.0),
        )
        c = _candidate(0.0, 50.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (10, 60))

        assert keep is True
        assert adjusted["start"] == 10.0  # first sentence skipped
        assert adjusted["end"] == 40.0    # last sentence trimmed
        assert call_count[0] == 1

    # ── Start-shift repair (fail + adj=0) ─────────────────────────────────────

    def test_fail_zero_adj_si_zero_drops_immediately(self, monkeypatch):
        """fail + adj=0 + si=0 → no earlier sentence; DROP with one call."""
        import producer.boundary_check as bc_mod
        fail_zero = (
            '{"verdict":"fail","reason":"hook/body mismatch",'
            '"adjusted_start_sentences":0,"adjusted_end_sentences":0}'
        )
        call_count = self._patch_client(monkeypatch, fail_zero)

        spans = _spans(
            ("The clip starts at the very beginning.", 0.0, 10.0),
            ("Body content about different topic.", 10.0, 25.0),
        )
        c = _candidate(0.0, 25.0)  # si will be 0 (first span)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (8, 60))

        assert keep is False
        assert call_count[0] == 1   # only first call; no start-shift (si=0)

    def test_fail_zero_adj_start_shift_pass_keeps(self, monkeypatch):
        """fail + adj=0 + si>0 → start-shift backward, second call passes → KEEP."""
        import producer.boundary_check as bc_mod
        fail_zero = (
            '{"verdict":"fail","reason":"starts mid-sentence",'
            '"adjusted_start_sentences":0,"adjusted_end_sentences":0}'
        )
        pass_verdict = (
            '{"verdict":"pass","reason":"now starts cleanly",'
            '"adjusted_start_sentences":0,"adjusted_end_sentences":0}'
        )
        call_count = self._patch_client(monkeypatch, fail_zero, pass_verdict)

        spans = _spans(
            ("Here is where the idea actually begins.", 0.0, 8.0),    # si-1 after shift
            ("And this is the mid-point of the thought.", 8.0, 18.0), # si (original start)
            ("The conclusion lands here.", 18.0, 28.0),               # ei
        )
        # Candidate starts at 8.0 → si=1, so there IS an earlier sentence (si-1=0)
        c = _candidate(8.0, 28.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (8, 60))

        assert keep is True
        assert adjusted["start"] == 0.0  # shifted back to span[0]
        assert call_count[0] == 2        # first verify + one start-shift verify

    def test_fail_zero_adj_start_shift_also_fails_drops(self, monkeypatch):
        """fail + adj=0 + si>0 → start-shift, second call also fails → DROP."""
        import producer.boundary_check as bc_mod
        fail_zero = (
            '{"verdict":"fail","reason":"whole span is wrong",'
            '"adjusted_start_sentences":0,"adjusted_end_sentences":0}'
        )
        call_count = self._patch_client(monkeypatch, fail_zero, fail_zero)

        spans = _spans(
            ("Some prior context sentence.", 0.0, 8.0),
            ("Clip starts here — wrong topic entirely.", 8.0, 20.0),
            ("More wrong-topic content.", 20.0, 32.0),
        )
        c = _candidate(8.0, 32.0)  # si=1, shift to si=0
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (8, 60))

        assert keep is False
        assert call_count[0] == 2   # first verify + one start-shift verify

    def test_fail_zero_adj_start_shift_pass_with_end_adj(self, monkeypatch):
        """fail + adj=0 + si>0 → shift, second call pass + adj_end=-1 → KEEP with trim."""
        import producer.boundary_check as bc_mod
        fail_zero = (
            '{"verdict":"fail","reason":"starts mid-thought",'
            '"adjusted_start_sentences":0,"adjusted_end_sentences":0}'
        )
        # Second call: pass but suggests trimming last sentence
        pass_with_adj = (
            '{"verdict":"pass","reason":"good start, trim tail",'
            '"adjusted_start_sentences":0,"adjusted_end_sentences":-1}'
        )
        call_count = self._patch_client(monkeypatch, fail_zero, pass_with_adj)

        spans = _spans(
            ("Real opening of the idea.", 0.0, 10.0),
            ("Continuation of the idea.", 10.0, 20.0),
            ("The payoff lands here.", 20.0, 30.0),
            ("Trailing list item: moving on.", 30.0, 40.0),
        )
        # Candidate starts at 10.0 → si=1; shift to si=0
        c = _candidate(10.0, 40.0)
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (8, 60))

        assert keep is True
        assert adjusted["start"] == 0.0   # shifted back to span[0]
        assert adjusted["end"] == 30.0    # trailing sentence trimmed by second call adj
        assert call_count[0] == 2

    def test_start_shift_transport_error_keeps_shifted(self, monkeypatch):
        """fail + adj=0 + si>0 → start-shift → second call throws → keep (non-fatal)."""
        import producer.boundary_check as bc_mod
        import core.llm as llm_mod

        call_count = [0]

        def fake_create_completion(client, model, max_tokens, messages):
            call_count[0] += 1
            if call_count[0] == 1:
                msg = MagicMock()
                msg.content = [MagicMock(
                    type="text",
                    text='{"verdict":"fail","reason":"start mid-sentence","adjusted_start_sentences":0,"adjusted_end_sentences":0}',
                )]
                return msg
            raise ConnectionError("network timeout on second call")

        mock_client = MagicMock()
        monkeypatch.setattr(bc_mod, "_get_boundary_client", lambda: (mock_client, "test-model"))
        monkeypatch.setattr(llm_mod, "create_completion", fake_create_completion)
        monkeypatch.setattr(llm_mod, "extract_text", lambda msg: msg.content[0].text)

        spans = _spans(
            ("Earlier sentence that is the real start.", 0.0, 10.0),
            ("This is where the ranker started.", 10.0, 22.0),
            ("More content here.", 22.0, 32.0),
        )
        c = _candidate(10.0, 32.0)  # si=1; shift possible
        adjusted, keep = bc_mod.verify_boundaries(c, spans, (8, 60))

        # Transport error on second call → non-fatal → keep
        assert keep is True
        assert call_count[0] == 2


# ---------------------------------------------------------------------------
# Sentence-index conversion tests (integration with _validate_moments)
# ---------------------------------------------------------------------------

class TestSentenceIndexConversion:
    """Verify that _validate_moments correctly converts start_sentence/end_sentence
    indices to float times using the provided sentence_spans."""

    def test_converts_indices_to_float_times(self):
        from core.llm import _validate_moments

        spans = [
            {"text": "Hello world.", "start": 0.0, "end": 5.0},
            {"text": "This is great.", "start": 5.0, "end": 10.0},
            {"text": "Final sentence.", "start": 10.0, "end": 15.0},
        ]
        raw = [{
            "start_sentence": 0,
            "end_sentence": 2,
            "score": 0.8,
            "hook": "Test hook",
            "reason": "good",
        }]
        result = _validate_moments(raw, (5, 60), sentence_spans=spans)
        assert len(result) == 1
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 15.0

    def test_clamps_out_of_range_indices(self):
        from core.llm import _validate_moments

        spans = [
            {"text": "First.", "start": 0.0, "end": 5.0},
            {"text": "Second.", "start": 5.0, "end": 10.0},
        ]
        raw = [{
            "start_sentence": -5,   # below 0
            "end_sentence": 999,    # beyond list
            "score": 0.9,
            "hook": "Hook",
            "reason": "",
        }]
        result = _validate_moments(raw, (5, 60), sentence_spans=spans)
        assert len(result) == 1
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 10.0

    def test_float_fallback_when_no_spans(self):
        from core.llm import _validate_moments

        raw = [{"start": 5.0, "end": 35.0, "score": 0.7, "hook": "Hook", "reason": ""}]
        result = _validate_moments(raw, (5, 60), sentence_spans=None)
        assert len(result) == 1
        assert result[0]["start"] == 5.0
        assert result[0]["end"] == 35.0

    def test_float_fallback_when_no_sentence_fields(self):
        """Even when spans are provided, old-shape (float) output is handled."""
        from core.llm import _validate_moments

        spans = [
            {"text": "First.", "start": 0.0, "end": 5.0},
            {"text": "Second.", "start": 5.0, "end": 10.0},
        ]
        raw = [{"start": 2.0, "end": 8.0, "score": 0.75, "hook": "Hook", "reason": ""}]
        result = _validate_moments(raw, (5, 60), sentence_spans=spans)
        assert len(result) == 1
        assert result[0]["start"] == 2.0
        assert result[0]["end"] == 8.0
