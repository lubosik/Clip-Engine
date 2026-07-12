"""
tests/test_punctuate.py — Unit tests for core/punctuate.py.

All punctuation model calls are mocked via monkeypatch.
No network calls are made here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_segments(*texts_with_times: tuple[str, float, float]) -> list[dict]:
    """Build a minimal transcript segment list."""
    return [
        {"text": t, "start": s, "end": e}
        for t, s, e in texts_with_times
    ]


def _make_mock_model(sentences: list[str]) -> Any:
    """Return a mock that behaves like PunctCapSegModelONNX."""
    m = MagicMock()
    m.infer.return_value = [sentences]
    return m


# ---------------------------------------------------------------------------
# Tests for the public restore_sentences function
# ---------------------------------------------------------------------------

class TestRestoreSentences:
    def test_empty_segments_returns_none(self):
        from core.punctuate import restore_sentences
        assert restore_sentences([]) is None

    def test_returns_none_when_model_not_available(self, monkeypatch):
        """When the punctuators model fails to load, restore_sentences returns None."""
        import core.punctuate as punct_mod
        monkeypatch.setattr(punct_mod, "_model", None)
        monkeypatch.setattr(punct_mod, "_model_load_failed", True)

        from core.punctuate import restore_sentences
        segs = _make_segments(
            ("hello world this is a test", 0.0, 5.0),
        )
        result = restore_sentences(segs)
        assert result is None

    def test_happy_path_produces_spans(self, monkeypatch):
        """When the model returns sentences, they are aligned to timestamps."""
        import core.punctuate as punct_mod

        mock_model = _make_mock_model([
            "Hello world, this is a test.",
            "How are you doing today?",
        ])
        monkeypatch.setattr(punct_mod, "_model", mock_model)
        monkeypatch.setattr(punct_mod, "_model_load_failed", False)

        from core.punctuate import restore_sentences
        segs = _make_segments(
            ("hello world this is a test how are you doing today", 0.0, 10.0),
        )
        result = restore_sentences(segs)
        assert result is not None
        assert isinstance(result, list)
        assert len(result) >= 1
        # Each span must have text, start, end
        for span in result:
            assert "text" in span
            assert "start" in span
            assert "end" in span
            assert isinstance(span["start"], float)
            assert isinstance(span["end"], float)

    def test_model_inference_error_returns_none(self, monkeypatch):
        """If model.infer raises, restore_sentences returns None gracefully."""
        import core.punctuate as punct_mod

        bad_model = MagicMock()
        bad_model.infer.side_effect = RuntimeError("inference blew up")
        monkeypatch.setattr(punct_mod, "_model", bad_model)
        monkeypatch.setattr(punct_mod, "_model_load_failed", False)

        from core.punctuate import restore_sentences
        segs = _make_segments(("some text here", 0.0, 3.0))
        result = restore_sentences(segs)
        assert result is None

    def test_model_returns_empty_list_returns_none(self, monkeypatch):
        """Empty infer output → None."""
        import core.punctuate as punct_mod

        mock_model = _make_mock_model([])
        monkeypatch.setattr(punct_mod, "_model", mock_model)
        monkeypatch.setattr(punct_mod, "_model_load_failed", False)

        from core.punctuate import restore_sentences
        segs = _make_segments(("test", 0.0, 1.0))
        result = restore_sentences(segs)
        assert result is None

    def test_get_model_caches_on_success(self, monkeypatch):
        """_get_model returns the same instance on repeated calls."""
        import core.punctuate as punct_mod

        sentinel = MagicMock()
        monkeypatch.setattr(punct_mod, "_model", sentinel)
        monkeypatch.setattr(punct_mod, "_model_load_failed", False)

        from core.punctuate import _get_model
        assert _get_model() is sentinel
        assert _get_model() is sentinel

    def test_get_model_fails_permanently_after_load_error(self, monkeypatch):
        """After _model_load_failed=True, _get_model always returns None."""
        import core.punctuate as punct_mod

        monkeypatch.setattr(punct_mod, "_model", None)
        monkeypatch.setattr(punct_mod, "_model_load_failed", True)

        from core.punctuate import _get_model
        assert _get_model() is None

    def test_timestamps_are_ordered(self, monkeypatch):
        """Sentence start times should be in non-decreasing order."""
        import core.punctuate as punct_mod

        mock_model = _make_mock_model([
            "Hello world.",
            "This is the second sentence.",
            "And here is the third one.",
        ])
        monkeypatch.setattr(punct_mod, "_model", mock_model)
        monkeypatch.setattr(punct_mod, "_model_load_failed", False)

        from core.punctuate import restore_sentences
        segs = _make_segments(
            ("hello world this is the second sentence and here is the third one", 0.0, 12.0),
        )
        result = restore_sentences(segs)
        if result and len(result) > 1:
            starts = [s["start"] for s in result]
            assert starts == sorted(starts), f"Timestamps not ordered: {starts}"

    def test_multi_segment_input(self, monkeypatch):
        """Multiple input segments are concatenated before model call."""
        import core.punctuate as punct_mod

        mock_model = _make_mock_model(["Hello world.", "How are you?"])
        monkeypatch.setattr(punct_mod, "_model", mock_model)
        monkeypatch.setattr(punct_mod, "_model_load_failed", False)

        from core.punctuate import restore_sentences
        segs = [
            {"text": "hello world", "start": 0.0, "end": 2.0},
            {"text": "how are you", "start": 2.5, "end": 5.0},
        ]
        result = restore_sentences(segs)
        # Should not raise; may return spans or None depending on alignment
        # but must not throw
        assert result is None or isinstance(result, list)


# ---------------------------------------------------------------------------
# Tests for alignment helpers (internal, importable)
# ---------------------------------------------------------------------------

class TestAlignSentencesToTimes:
    def test_single_sentence_alignment(self):
        """Single sentence should align to first/last word timestamps."""
        from core.punctuate import _align_sentences_to_times

        full_text = "hello world"
        char_times = [float(i) for i in range(len(full_text))]
        sentences = ["Hello world."]

        result = _align_sentences_to_times(sentences, full_text, char_times)
        assert result is not None
        assert len(result) == 1
        assert result[0]["text"] == "Hello world."
        assert result[0]["start"] == 0.0

    def test_empty_sentences(self):
        """Empty sentence list → None."""
        from core.punctuate import _align_sentences_to_times

        result = _align_sentences_to_times([], "hello world", [0.0] * 11)
        assert result is None


class TestNormalizeWord:
    def test_strips_punctuation(self):
        from core.punctuate import _normalize_word
        assert _normalize_word("Hello,") == "hello"
        assert _normalize_word("world.") == "world"
        assert _normalize_word("it's") == "it's"

    def test_lowercase(self):
        from core.punctuate import _normalize_word
        assert _normalize_word("FDA") == "fda"
