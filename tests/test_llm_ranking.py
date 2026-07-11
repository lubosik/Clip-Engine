"""
tests/test_llm_ranking.py — cost-optimisation helpers in core/llm.

Covers transcript compression (input-token reduction for the prompt) and the
combined segmentation+ranking response parser (one call returns topics + clips).
"""

from __future__ import annotations

from core.llm import (
    _compress_transcript,
    _parse_ranking_response,
    create_completion,
    extract_text,
)


class _Block:
    def __init__(self, type_, text=None):
        self.type = type_
        if text is not None:
            self.text = text


class _Msg:
    def __init__(self, content):
        self.content = content


class TestExtractText:
    def test_plain_text_block(self):
        assert extract_text(_Msg([_Block("text", "hello")])) == "hello"

    def test_skips_leading_thinking_block(self):
        # Thinking-on models (Sonnet 5, Opus 4.8) put a ThinkingBlock at [0].
        msg = _Msg([_Block("thinking"), _Block("text", "the answer")])
        assert extract_text(msg) == "the answer"

    def test_empty_content(self):
        assert extract_text(_Msg([])) == ""
        assert extract_text(_Msg(None)) == ""


class TestCreateCompletion:
    def test_disables_thinking(self):
        calls = []

        class Client:
            class messages:
                @staticmethod
                def create(**kwargs):
                    calls.append(kwargs)
                    return _Msg([_Block("text", "ok")])

        create_completion(Client(), "m", 512, [{"role": "user", "content": "x"}])
        assert calls[0]["thinking"] == {"type": "disabled"}

    def test_falls_back_when_thinking_rejected(self):
        attempts = []

        class Client:
            class messages:
                @staticmethod
                def create(**kwargs):
                    attempts.append("thinking" in kwargs)
                    if "thinking" in kwargs:
                        raise ValueError("thinking is not supported on this model")
                    return _Msg([_Block("text", "ok")])

        msg = create_completion(Client(), "m", 512, [{"role": "user", "content": "x"}])
        assert extract_text(msg) == "ok"
        assert attempts == [True, False]  # tried with thinking, retried without

    def test_non_thinking_error_propagates(self):
        class Client:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise ValueError("rate limit exceeded")

        import pytest

        with pytest.raises(ValueError, match="rate limit"):
            create_completion(Client(), "m", 512, [{"role": "user", "content": "x"}])


class TestCompressTranscript:
    def test_merges_short_fragments_into_chunks(self):
        # 8 fragments of 3s each → chunks close each time they reach ~12s.
        frags = [
            {"start": i * 3.0, "end": i * 3.0 + 3.0, "text": f"word{i}"}
            for i in range(8)
        ]
        out = _compress_transcript(frags, target_s=12.0)
        # 24s of content in ~12s chunks → 2 chunks.
        assert len(out) == 2
        assert out[0]["start"] == 0.0
        assert out[0]["end"] == 12.0
        # Text is preserved verbatim, space-joined.
        assert out[0]["text"] == "word0 word1 word2 word3"
        assert out[1]["text"] == "word4 word5 word6 word7"

    def test_no_word_loss(self):
        frags = [
            {"start": 0.0, "end": 2.0, "text": "the peptide"},
            {"start": 2.0, "end": 4.0, "text": "helps with"},
            {"start": 4.0, "end": 30.0, "text": "healing after an injury."},
        ]
        out = _compress_transcript(frags, target_s=12.0)
        joined = " ".join(c["text"] for c in out)
        assert "the peptide helps with healing after an injury." == joined

    def test_skips_empty_and_bad_segments(self):
        frags = [
            {"start": 0.0, "end": 2.0, "text": ""},
            {"start": 2.0, "end": 4.0, "text": "   "},
            {"start": "bad", "end": 4.0, "text": "x"},
            {"start": 4.0, "end": 6.0, "text": "real"},
        ]
        out = _compress_transcript(frags, target_s=12.0)
        assert len(out) == 1
        assert out[0]["text"] == "real"

    def test_empty_input(self):
        assert _compress_transcript([]) == []

    def test_trailing_partial_chunk_kept(self):
        frags = [{"start": 0.0, "end": 5.0, "text": "only one short chunk"}]
        out = _compress_transcript(frags, target_s=12.0)
        assert len(out) == 1
        assert out[0]["text"] == "only one short chunk"


class TestParseRankingResponse:
    def test_object_with_topics_and_clips(self):
        text = (
            'Here you go:\n{"topics": [{"start": 0, "end": 30, "summary": "intro"}], '
            '"clips": [{"start": 5, "end": 25, "score": 0.8, "hook": "h", "reason": "r"}]}'
        )
        clips, topics = _parse_ranking_response(text)
        assert len(clips) == 1 and clips[0]["hook"] == "h"
        assert len(topics) == 1 and topics[0]["summary"] == "intro"

    def test_object_clips_only(self):
        text = '{"clips": [{"start": 0, "end": 20, "score": 0.5, "hook": "x", "reason": "y"}]}'
        clips, topics = _parse_ranking_response(text)
        assert len(clips) == 1
        assert topics == []

    def test_bare_array_fallback(self):
        # Legacy / model slip: a bare array is treated as clips, no topics.
        text = '[{"start": 0, "end": 20, "score": 0.5, "hook": "x", "reason": "y"}]'
        clips, topics = _parse_ranking_response(text)
        assert len(clips) == 1
        assert topics == []

    def test_empty_object(self):
        clips, topics = _parse_ranking_response('{"topics": [], "clips": []}')
        assert clips == [] and topics == []

    def test_unparseable(self):
        clips, topics = _parse_ranking_response("sorry, no JSON here")
        assert clips == [] and topics == []
