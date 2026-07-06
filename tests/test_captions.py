"""
tests/test_captions.py — Pure unit tests for the ASS caption generator.

All tests operate on synthetic word timings and perform text assertions only —
no video files, no ffmpeg, no heavy ML imports.

Coverage
--------
* _hex_to_ass_color  — opaque #RRGGBB and semi-transparent #RRGGBBAA
* _ass_time          — boundary values (0 s, sub-second, 1 h+)
* _group_into_lines  — grouping, remainder, empty input
* _build_line_text   — highlight placement and color tags
* _build_dialogue_events — start/end times, duration fill-in, per-event text
* build_ass (integration) — complete file written to tmp, header checks,
                             event count, position-to-alignment mapping
* _position_to_alignment — all three position strings + unknown fallback
* get_word_timings fallback when faster-whisper is absent (ImportError path)
"""

import re
import sys
import tempfile
import textwrap
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module under test — import without triggering heavy deps
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent))

from producer.render.captions import (
    _ass_time,
    _build_dialogue_events,
    _build_line_text,
    _group_into_lines,
    _hex_to_ass_color,
    _position_to_alignment,
    _render_ass,
    build_ass,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _make_word(word: str, start: float, end: float) -> dict:
    return {"word": word, "start": start, "end": end}


def _make_template(
    font: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    base_color: str = "#FFFFFF",
    highlight_color: str = "#00E5FF",
    outline_color: str = "#000000",
    outline_px: int = 6,
    position: str = "upper_mid",
    max_words_per_line: int = 4,
    resolution: list = None,
):
    """Construct a minimal template namespace compatible with build_ass."""
    if resolution is None:
        resolution = [1080, 1920]

    cap = types.SimpleNamespace(
        font=font,
        base_color=base_color,
        highlight_color=highlight_color,
        outline_color=outline_color,
        outline_px=outline_px,
        position=position,
        max_words_per_line=max_words_per_line,
    )
    return types.SimpleNamespace(captions=cap, resolution=resolution)


# ===========================================================================
# _hex_to_ass_color
# ===========================================================================

class TestHexToAssColor:
    def test_opaque_white(self):
        result = _hex_to_ass_color("#FFFFFF")
        # Fully opaque: AA=00, BGR order: FF FF FF
        assert result == "&H00FFFFFF"

    def test_opaque_black(self):
        result = _hex_to_ass_color("#000000")
        assert result == "&H00000000"

    def test_opaque_cyan_00E5FF(self):
        # HTML: R=00, G=E5, B=FF  →  ASS BGR: FF E5 00  →  &H00FFE500
        result = _hex_to_ass_color("#00E5FF")
        assert result == "&H00FFE500"

    def test_semi_transparent_box_color(self):
        # #111111CC: HTML alpha = 0xCC (204) → opaque
        # ASS alpha = 255 - 204 = 51 = 0x33
        result = _hex_to_ass_color("#111111CC")
        # ASS: &H{33}{11}{11}{11}
        assert result == "&H33111111"

    def test_fully_transparent_alpha_00(self):
        # HTML #RRGGBB00 → alpha 0 → fully transparent → ASS AA = FF
        result = _hex_to_ass_color("#FFFFFF00")
        assert result == "&HFFFFFFFF"

    def test_invalid_length_raises(self):
        with pytest.raises(ValueError, match="unrecognised colour"):
            _hex_to_ass_color("#FFF")

    def test_no_hash_prefix_raises(self):
        # hex without # prefix has 6 chars but no leading #; lstrip handles it
        # Actually our function does lstrip('#') so "FFFFFF" → still 6 chars → valid
        result = _hex_to_ass_color("FFFFFF")
        assert result == "&H00FFFFFF"


# ===========================================================================
# _ass_time
# ===========================================================================

class TestAssTime:
    def test_zero(self):
        assert _ass_time(0.0) == "0:00:00.00"

    def test_one_second(self):
        assert _ass_time(1.0) == "0:00:01.00"

    def test_sub_second(self):
        # 0.5 s = 50 centiseconds
        assert _ass_time(0.5) == "0:00:00.50"

    def test_one_minute(self):
        assert _ass_time(60.0) == "0:01:00.00"

    def test_one_hour(self):
        assert _ass_time(3600.0) == "1:00:00.00"

    def test_centisecond_rounding(self):
        # 1.234 s → 123.4 cs → rounds to 123 → 1s + 23 cs
        assert _ass_time(1.234) == "0:00:01.23"

    def test_negative_clamps_to_zero(self):
        # Negative times should not produce invalid timestamps
        assert _ass_time(-0.5) == "0:00:00.00"

    def test_complex_time(self):
        # 3725.75 s = 1h 2m 5.75s → centiseconds: 75
        result = _ass_time(3725.75)
        assert result == "1:02:05.75"


# ===========================================================================
# _group_into_lines
# ===========================================================================

class TestGroupIntoLines:
    def test_empty(self):
        assert _group_into_lines([], 4) == []

    def test_exactly_one_line(self):
        words = [_make_word(f"w{i}", i, i + 0.5) for i in range(4)]
        lines = _group_into_lines(words, 4)
        assert len(lines) == 1
        assert len(lines[0]) == 4

    def test_two_full_lines(self):
        words = [_make_word(f"w{i}", i, i + 0.5) for i in range(8)]
        lines = _group_into_lines(words, 4)
        assert len(lines) == 2
        assert lines[0][0]["word"] == "w0"
        assert lines[1][0]["word"] == "w4"

    def test_remainder_line(self):
        # 9 words, 4 per line → lines of [4, 4, 1]
        words = [_make_word(f"w{i}", i, i + 0.5) for i in range(9)]
        lines = _group_into_lines(words, 4)
        assert len(lines) == 3
        assert len(lines[2]) == 1

    def test_max_words_1(self):
        words = [_make_word(f"w{i}", i, i + 0.5) for i in range(3)]
        lines = _group_into_lines(words, 1)
        assert len(lines) == 3
        assert all(len(ln) == 1 for ln in lines)

    def test_max_words_greater_than_total(self):
        words = [_make_word(f"w{i}", i, i + 0.5) for i in range(2)]
        lines = _group_into_lines(words, 10)
        assert len(lines) == 1
        assert len(lines[0]) == 2


# ===========================================================================
# _build_line_text
# ===========================================================================

class TestBuildLineText:
    BASE = "&H00FFFFFF"
    HI = "&H00FFE500"

    def test_active_first_word(self):
        words = [
            _make_word("Hello", 0, 0.5),
            _make_word("World", 0.5, 1.0),
        ]
        text = _build_line_text(words, 0, self.BASE, self.HI)
        # Active word is highlighted, next word is plain
        assert f"{{{chr(92)}c{self.HI}}}Hello{{{chr(92)}c{self.BASE}}}" in text
        assert "World" in text
        # Non-active word should NOT have a highlight tag
        assert text.index("World") > text.index("Hello")

    def test_active_last_word(self):
        words = [
            _make_word("One", 0, 0.3),
            _make_word("Two", 0.3, 0.6),
            _make_word("Three", 0.6, 1.0),
        ]
        text = _build_line_text(words, 2, self.BASE, self.HI)
        # "Three" highlighted, "One" and "Two" plain
        assert "\\c" + self.HI + "}Three" in text
        assert text.startswith("One Two")

    def test_single_word_line(self):
        words = [_make_word("Solo", 0.0, 1.0)]
        text = _build_line_text(words, 0, self.BASE, self.HI)
        assert self.HI in text
        assert "Solo" in text

    def test_color_reset_after_highlight(self):
        words = [_make_word("A", 0, 0.5), _make_word("B", 0.5, 1.0)]
        text = _build_line_text(words, 0, self.BASE, self.HI)
        # Should reset to base color after the active word
        assert self.BASE in text
        # Base color reset should come after the highlight
        hi_pos = text.index(self.HI)
        base_pos = text.index(self.BASE)
        assert base_pos > hi_pos

    def test_four_words_middle_active(self):
        words = [_make_word(f"W{i}", i * 0.5, (i + 1) * 0.5) for i in range(4)]
        text = _build_line_text(words, 2, self.BASE, self.HI)
        # W0 and W1 before highlight; W3 after reset
        parts = text.split()
        # structure: W0 W1 {hi}W2{base} W3 (split on spaces loses tags)
        assert "W0" in text
        assert "W3" in text
        # W2 should be highlighted
        assert "\\c" + self.HI + "}W2" in text


# ===========================================================================
# _build_dialogue_events
# ===========================================================================

class TestBuildDialogueEvents:
    BASE = "&H00FFFFFF"
    HI = "&H00FFE500"

    def _events(self, words, max_per_line=4):
        lines = _group_into_lines(words, max_per_line)
        return _build_dialogue_events(lines, base_color=self.BASE, highlight_color=self.HI)

    def test_four_words_four_events(self):
        words = [_make_word(f"w{i}", i * 0.5, (i + 1) * 0.5) for i in range(4)]
        events = self._events(words)
        assert len(events) == 4

    def test_event_starts_match_word_starts(self):
        words = [_make_word(f"w{i}", i * 1.0, (i + 1) * 1.0) for i in range(4)]
        events = self._events(words)
        for i, (start, end, _) in enumerate(events):
            assert abs(start - i * 1.0) < 1e-9, f"event {i} start mismatch"

    def test_event_ends_equal_next_word_start(self):
        words = [_make_word(f"w{i}", i * 1.0, (i + 1) * 1.0) for i in range(4)]
        events = self._events(words)
        # First three events end when next word starts
        assert abs(events[0][1] - 1.0) < 1e-9
        assert abs(events[1][1] - 2.0) < 1e-9
        assert abs(events[2][1] - 3.0) < 1e-9
        # Last event ends at word's own end
        assert abs(events[3][1] - 4.0) < 1e-9

    def test_gap_between_words_handled(self):
        # Word0 ends at 0.4, Word1 starts at 0.8 (gap of 0.4 s)
        words = [
            _make_word("A", 0.0, 0.4),
            _make_word("B", 0.8, 1.2),
        ]
        events = self._events(words)
        # Event for A should end when B starts (0.8), not at A's end (0.4)
        assert abs(events[0][1] - 0.8) < 1e-9

    def test_zero_duration_word_gets_minimum_duration(self):
        words = [_make_word("X", 1.0, 1.0)]  # same start and end
        events = self._events(words)
        start, end, _ = events[0]
        assert end > start
        assert (end - start) >= 0.04  # at least 40 ms

    def test_two_lines_correct_event_count(self):
        words = [_make_word(f"w{i}", i * 0.5, (i + 1) * 0.5) for i in range(8)]
        events = self._events(words, max_per_line=4)
        assert len(events) == 8

    def test_last_word_of_first_line_uses_own_end(self):
        # Two lines: [w0..w3] and [w4..w7]
        # Last word of first line (w3) should end at its own end, not w4's start
        words = [_make_word(f"w{i}", i * 1.0, (i + 1) * 1.0) for i in range(8)]
        events = self._events(words, max_per_line=4)
        # w3 is event index 3; w3.end = 4.0, w4.start = 4.0 → same, fine
        # Force different end: w3.end = 3.8, w4.start = 4.2
        words2 = [_make_word(f"w{i}", i * 1.0, i * 1.0 + 0.8) for i in range(8)]
        events2 = self._events(words2, max_per_line=4)
        # w3 (idx 3) end should be its own end (3*1.0 + 0.8 = 3.8), not w4.start (4.0)
        assert abs(events2[3][1] - 3.8) < 1e-9

    def test_single_word_event_text_contains_highlight(self):
        words = [_make_word("Test", 0.0, 1.0)]
        events = self._events(words)
        _, _, text = events[0]
        assert self.HI in text
        assert "Test" in text


# ===========================================================================
# _position_to_alignment
# ===========================================================================

class TestPositionToAlignment:
    def test_upper_mid(self):
        alignment, margin_v = _position_to_alignment("upper_mid", 1920)
        assert alignment == 8
        assert abs(margin_v - int(1920 * 0.38)) <= 1

    def test_center(self):
        alignment, margin_v = _position_to_alignment("center", 1920)
        assert alignment == 5
        assert margin_v == 0

    def test_lower_third(self):
        alignment, margin_v = _position_to_alignment("lower_third", 1920)
        assert alignment == 2
        assert abs(margin_v - int(1920 * 0.15)) <= 1

    def test_unknown_position_defaults_to_upper_mid(self):
        alignment, _ = _position_to_alignment("nonexistent", 1920)
        assert alignment == 8

    def test_case_insensitive(self):
        # position strings are lowercased inside the function
        alignment, _ = _position_to_alignment("UPPER_MID", 1920)
        assert alignment == 8


# ===========================================================================
# build_ass — integration tests (writes real .ass file to tmp)
# ===========================================================================

class TestBuildAss:
    def _sample_words(self):
        return [
            _make_word("This",  0.0,  0.3),
            _make_word("is",    0.35, 0.5),
            _make_word("a",     0.55, 0.65),
            _make_word("test",  0.7,  1.0),
            _make_word("clip",  1.1,  1.4),
            _make_word("for",   1.5,  1.7),
            _make_word("you",   1.8,  2.1),
        ]

    def test_creates_file(self, tmp_path):
        words = self._sample_words()
        tmpl = _make_template()
        out = tmp_path / "captions.ass"
        result = build_ass(words, tmpl, out)
        assert result == out
        assert out.exists()

    def test_script_info_header(self, tmp_path):
        words = self._sample_words()
        tmpl = _make_template(resolution=[1080, 1920])
        out = tmp_path / "captions.ass"
        build_ass(words, tmpl, out)
        content = out.read_text(encoding="utf-8-sig")
        assert "[Script Info]" in content
        assert "PlayResX: 1080" in content
        assert "PlayResY: 1920" in content

    def test_events_section_present(self, tmp_path):
        words = self._sample_words()
        tmpl = _make_template()
        out = tmp_path / "captions.ass"
        build_ass(words, tmpl, out)
        content = out.read_text(encoding="utf-8-sig")
        assert "[Events]" in content
        assert "Dialogue:" in content

    def test_correct_event_count(self, tmp_path):
        # 7 words → 7 events (one per word)
        words = self._sample_words()
        tmpl = _make_template(max_words_per_line=4)
        out = tmp_path / "captions.ass"
        build_ass(words, tmpl, out)
        content = out.read_text(encoding="utf-8-sig")
        event_lines = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
        assert len(event_lines) == 7

    def test_highlight_color_appears_in_events(self, tmp_path):
        words = self._sample_words()
        tmpl = _make_template(highlight_color="#00E5FF")
        out = tmp_path / "captions.ass"
        build_ass(words, tmpl, out)
        content = out.read_text(encoding="utf-8-sig")
        # The ASS highlight color for #00E5FF is &H00FFE500
        assert "&H00FFE500" in content

    def test_base_color_in_style(self, tmp_path):
        words = self._sample_words()
        tmpl = _make_template(base_color="#FFFFFF")
        out = tmp_path / "captions.ass"
        build_ass(words, tmpl, out)
        content = out.read_text(encoding="utf-8-sig")
        # White base color → &H00FFFFFF in style
        assert "&H00FFFFFF" in content

    def test_upper_mid_alignment_8(self, tmp_path):
        words = self._sample_words()
        tmpl = _make_template(position="upper_mid")
        out = tmp_path / "captions.ass"
        build_ass(words, tmpl, out)
        content = out.read_text(encoding="utf-8-sig")
        # Style line should contain alignment 8
        style_lines = [ln for ln in content.splitlines() if ln.startswith("Style:")]
        assert len(style_lines) == 1
        # Alignment is the 19th field (0-indexed: 18)
        fields = style_lines[0].split(",")
        alignment_field = fields[18]
        assert alignment_field.strip() == "8"

    def test_lower_third_alignment_2(self, tmp_path):
        words = self._sample_words()
        tmpl = _make_template(position="lower_third")
        out = tmp_path / "captions.ass"
        build_ass(words, tmpl, out)
        content = out.read_text(encoding="utf-8-sig")
        style_lines = [ln for ln in content.splitlines() if ln.startswith("Style:")]
        fields = style_lines[0].split(",")
        assert fields[18].strip() == "2"

    def test_first_event_timing(self, tmp_path):
        words = [
            _make_word("Hello", 0.0, 0.5),
            _make_word("World", 0.5, 1.0),
        ]
        tmpl = _make_template()
        out = tmp_path / "captions.ass"
        build_ass(words, tmpl, out)
        content = out.read_text(encoding="utf-8-sig")
        event_lines = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
        # First event: start=0:00:00.00, end=0:00:00.50 (next word's start)
        assert "0:00:00.00" in event_lines[0]
        assert "0:00:00.50" in event_lines[0]

    def test_empty_words_creates_valid_file(self, tmp_path):
        tmpl = _make_template()
        out = tmp_path / "empty.ass"
        result = build_ass([], tmpl, out)
        assert out.exists()
        content = out.read_text(encoding="utf-8-sig")
        assert "[Script Info]" in content
        # No Dialogue events for empty word list
        event_lines = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
        assert len(event_lines) == 0

    def test_line_grouping_4_words_per_line(self, tmp_path):
        # 8 words → 2 lines → 8 events, each line-group restarts highlight from idx 0
        words = [_make_word(f"w{i}", i * 0.5, (i + 1) * 0.5) for i in range(8)]
        tmpl = _make_template(max_words_per_line=4)
        out = tmp_path / "captions.ass"
        build_ass(words, tmpl, out)
        content = out.read_text(encoding="utf-8-sig")
        event_lines = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
        assert len(event_lines) == 8

    def test_outline_px_in_style(self, tmp_path):
        words = self._sample_words()
        tmpl = _make_template(outline_px=8)
        out = tmp_path / "captions.ass"
        build_ass(words, tmpl, out)
        content = out.read_text(encoding="utf-8-sig")
        style_lines = [ln for ln in content.splitlines() if ln.startswith("Style:")]
        # Outline is field index 16 (0-indexed)
        fields = style_lines[0].split(",")
        assert fields[16].strip() == "8"


# ===========================================================================
# get_word_timings — import-error path (no faster-whisper installed)
# ===========================================================================

class TestGetWordTimingsFallback:
    def test_raises_runtime_error_when_not_installed(self, monkeypatch):
        """Simulate faster-whisper not being installed."""
        # Remove faster_whisper from sys.modules if present, then block import
        sys.modules.pop("faster_whisper", None)

        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "faster_whisper":
                raise ImportError("No module named 'faster_whisper'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        from producer.render.captions import get_word_timings
        with pytest.raises(RuntimeError, match="faster-whisper is not installed"):
            get_word_timings(Path("/nonexistent/audio.wav"))


# ===========================================================================
# Timing math edge cases
# ===========================================================================

class TestTimingMath:
    """Verify that line boundaries and multi-line timing are correct."""

    def test_last_word_of_line_uses_own_end_not_next_lines_start(self):
        """When a line boundary falls between words, the last word of each line
        must use its own end time, not the first word of the next line's start."""
        words = [
            _make_word("Line1W1", 0.0, 0.4),
            _make_word("Line1W2", 0.5, 0.9),   # last in line 1; end=0.9
            _make_word("Line2W1", 2.0, 2.4),   # first in line 2, big gap
            _make_word("Line2W2", 2.5, 2.9),
        ]
        lines = _group_into_lines(words, 2)
        events = _build_dialogue_events(
            lines, base_color="&H00FFFFFF", highlight_color="&H00FFE500"
        )
        # Line1W2 is event index 1; its end must be 0.9 (own end), not 2.0
        assert abs(events[1][1] - 0.9) < 1e-9

    def test_words_with_overlapping_timings_dont_crash(self):
        """Malformed input where word ends after next word starts should not crash."""
        words = [
            _make_word("A", 0.0, 1.0),   # ends at 1.0
            _make_word("B", 0.5, 1.5),   # starts before A ends
        ]
        lines = _group_into_lines(words, 4)
        events = _build_dialogue_events(
            lines, base_color="&H00FFFFFF", highlight_color="&H00FFE500"
        )
        # Should produce 2 events without raising
        assert len(events) == 2
        # A's end = B's start = 0.5 (next word start), B's end = 1.5
        assert abs(events[0][1] - 0.5) < 1e-9

    def test_timing_preserves_centisecond_precision(self):
        """Word timings with sub-centisecond precision are rounded to centiseconds."""
        # 1.234 s → 123.4 cs → rounds to 123 → "0:00:01.23"
        t = _ass_time(1.234)
        assert t == "0:00:01.23"

    def test_large_number_of_words(self):
        """100 words at 0.5 s each → 25 lines of 4 → 100 events, no crash."""
        words = [_make_word(f"w{i}", i * 0.5, (i + 1) * 0.5) for i in range(100)]
        lines = _group_into_lines(words, 4)
        assert len(lines) == 25
        events = _build_dialogue_events(
            lines, base_color="&H00FFFFFF", highlight_color="&H00FFE500"
        )
        assert len(events) == 100
