"""
tests/test_hook_wrap.py — Unit tests for hook-text wrap and shrink-to-fit logic.

Covers _wrap_text and _compute_hook_fit without ffmpeg, Modal, or GPU.
The modal package is stubbed before import so the test runner does not need
the Modal SDK installed or authenticated.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Stub 'modal' before importing render.modal_app — the SDK is only needed on
# the GPU worker container, not in tests.
sys.modules.setdefault("modal", MagicMock())

from render.modal_app import _compute_hook_fit, _wrap_text  # noqa: E402


# ---------------------------------------------------------------------------
# _wrap_text
# ---------------------------------------------------------------------------

class TestWrapText:
    def test_short_text_single_line(self):
        assert _wrap_text("Hello world", 20) == "Hello world"

    def test_wraps_at_char_limit(self):
        text = "The quick brown fox jumps over"
        lines = _wrap_text(text, 12).split("\n")
        for line in lines:
            # Each line must be at or under the char limit, unless it is a
            # single word that is itself longer than the limit.
            assert len(line) <= 12 or " " not in line

    def test_no_words_lost(self):
        text = (
            "Every time you work out you are actually damaging your muscles "
            "and that is a good thing"
        )
        wrapped = _wrap_text(text, 22)
        for word in text.split():
            assert word in wrapped

    def test_empty_string(self):
        assert _wrap_text("", 20) == ""

    def test_single_word(self):
        assert _wrap_text("incredible", 5) == "incredible"


# ---------------------------------------------------------------------------
# _compute_hook_fit
# ---------------------------------------------------------------------------

class TestComputeHookFit:
    """_compute_hook_fit must never truncate or append ellipsis."""

    # Standard 9:16 render dimensions
    W, H = 1080, 1920

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _words_intact(original: str, lines: list[str]) -> bool:
        combined = " ".join(lines)
        return all(word in combined for word in original.split())

    # ------------------------------------------------------------------
    # no-ellipsis guarantee
    # ------------------------------------------------------------------

    def test_no_ellipsis_very_short_hook(self):
        hook = "This is incredible!"
        lines, _ = _compute_hook_fit(hook, self.W, self.H)
        assert "..." not in "\n".join(lines)

    def test_no_ellipsis_typical_hook(self):
        # Real hook from the first successful demo run
        hook = (
            "Every time you work out, you're actually damaging your muscles "
            "— and that's a good thing."
        )
        lines, _ = _compute_hook_fit(hook, self.W, self.H)
        assert "..." not in "\n".join(lines)

    def test_no_ellipsis_long_hook(self):
        hook = (
            "The one thing no personal trainer will ever tell you about why "
            "most people fail to build muscle even when they work out every single day"
        )
        lines, _ = _compute_hook_fit(hook, self.W, self.H)
        assert "..." not in "\n".join(lines)

    # ------------------------------------------------------------------
    # all words preserved
    # ------------------------------------------------------------------

    def test_all_words_preserved_short(self):
        hook = "Short hook for testing purposes."
        lines, _ = _compute_hook_fit(hook, self.W, self.H)
        assert self._words_intact(hook, lines)

    def test_all_words_preserved_typical(self):
        hook = (
            "Every time you work out, you're actually damaging your muscles "
            "— and that's a good thing."
        )
        lines, _ = _compute_hook_fit(hook, self.W, self.H)
        assert self._words_intact(hook, lines)

    def test_all_words_preserved_long(self):
        hook = (
            "The one thing no personal trainer will ever tell you about why "
            "most people fail to build muscle even when they work out every single day"
        )
        lines, _ = _compute_hook_fit(hook, self.W, self.H)
        assert self._words_intact(hook, lines)

    # ------------------------------------------------------------------
    # font-size constraints
    # ------------------------------------------------------------------

    def test_font_size_at_or_above_floor(self):
        for hook in [
            "Go.",
            "Short hook text here.",
            "Every time you work out you are actually damaging your muscles and that is a good thing",
            "The one thing no trainer ever tells you about why people fail to build muscle",
        ]:
            _, fs = _compute_hook_fit(hook, self.W, self.H)
            assert fs >= 30, f"font size {fs} is below floor for hook: {hook!r}"

    def test_font_size_does_not_exceed_base(self):
        base_fs = max(44, int(self.H * 0.038))  # 72 for 1920px height
        hook = "Short hook."
        _, fs = _compute_hook_fit(hook, self.W, self.H)
        assert fs <= base_fs

    def test_font_shrinks_for_longer_hook(self):
        short_hook = "Go."
        long_hook = (
            "The one thing no personal trainer will ever tell you about why "
            "most people fail to build muscle even when they work out every single day"
        )
        _, fs_short = _compute_hook_fit(short_hook, self.W, self.H)
        _, fs_long = _compute_hook_fit(long_hook, self.W, self.H)
        assert fs_long <= fs_short

    # ------------------------------------------------------------------
    # width constraint: each line must fit within 0.92 * out_w
    # ------------------------------------------------------------------

    def test_each_line_fits_within_frame_width(self):
        hooks = [
            "This is incredible!",
            "Every time you work out, you're actually damaging your muscles — and that's a good thing.",
            "The one thing no personal trainer will ever tell you about why most people fail.",
        ]
        for hook in hooks:
            lines, fs = _compute_hook_fit(hook, self.W, self.H)
            px_budget = self.W * 0.92
            for line in lines:
                approx_px = len(line) * 0.60 * fs
                assert approx_px <= px_budget + 1, (
                    f"hook={hook!r}\nline={line!r}\n"
                    f"approx_px={approx_px:.1f} > budget={px_budget:.1f}"
                )
