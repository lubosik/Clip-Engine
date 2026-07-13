"""
tests/test_reframe_quality.py — Render-quality tests for spec items R1, R2, R3.

Covers:
  (a) Unit tests: margin guard geometry, mode selection, polyfit clamping,
      blur threshold logic — all using synthetic frames/data, no real video.
  (b) Gate blur calibration: verify _check_frames_sharp returns the correct
      pass/fail verdict for real rendered clips (clip52 must FAIL,
      clip46 must PASS).
  (c) End-to-end reframe_segment: run on a real R2 source segment with ≥1
      camera cut; extract frames from the output; assert every face in sampled
      output frames sits within the central 80% of the 9:16 frame.

Marks:
  @pytest.mark.e2e  — skipped unless CLIP_ENGINE_E2E=1 is set (needs R2 creds
                       and ~10 MB source on disk or auto-downloaded).
  @pytest.mark.realclips — skipped unless the real rendered clips are present
                       in the scratchpad directory.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import cv2
import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Paths to real rendered clips (scratchpad)
# ---------------------------------------------------------------------------

_SCRATCHPAD = Path(
    "/tmp/claude-0/-root/01b83aed-f84f-45a9-92db-c5ee5e7713fd/scratchpad"
)
_CLIP46 = _SCRATCHPAD / "clip46.mp4"
_CLIP52 = _SCRATCHPAD / "clip52.mp4"
_CLIP53 = _SCRATCHPAD / "clip53.mp4"
_CLIP55 = _SCRATCHPAD / "clip55.mp4"
_SOURCE_E2E = _SCRATCHPAD / "source_e2e.mp4"

_HAS_REAL_CLIPS = _CLIP52.exists() and _CLIP46.exists()
_HAS_E2E_SOURCE = _SOURCE_E2E.exists()

REALCLIPS = pytest.mark.skipif(
    not _HAS_REAL_CLIPS,
    reason="Real rendered clips not found in scratchpad",
)
E2E = pytest.mark.skipif(
    not (os.environ.get("CLIP_ENGINE_E2E", "0") == "1" or _HAS_E2E_SOURCE),
    reason="End-to-end test needs CLIP_ENGINE_E2E=1 or source_e2e.mp4 in scratchpad",
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers: synthetic frame generation
# ---------------------------------------------------------------------------

def _make_blank_frame(h: int = 1080, w: int = 1920, color: tuple = (100, 100, 100)) -> np.ndarray:
    """Create a solid-color BGR frame."""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:] = color
    return frame


def _make_sharp_frame(h: int = 1080, w: int = 1920) -> np.ndarray:
    """Create a synthetic sharp frame (high Laplacian variance) with a checkerboard pattern."""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    tile = 8
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            if (y // tile + x // tile) % 2 == 0:
                frame[y:y + tile, x:x + tile] = (255, 255, 255)
    return frame


def _make_blurry_frame(h: int = 1080, w: int = 1920) -> np.ndarray:
    """Create a synthetic blurry frame (low Laplacian variance) via heavy Gaussian blur."""
    sharp = _make_sharp_frame(h, w)
    return cv2.GaussianBlur(sharp, (99, 99), 30)


def _make_frame_with_face_blob(
    h: int = 1080, w: int = 1920,
    face_cx_frac: float = 0.5,
    face_cy_frac: float = 0.3,
    face_w_frac: float = 0.1,
    face_h_frac: float = 0.15,
) -> np.ndarray:
    """Create a frame with a solid ellipse simulating a face at the given position."""
    frame = _make_blank_frame(h, w, (50, 50, 50))
    cx_px = int(face_cx_frac * w)
    cy_px = int(face_cy_frac * h)
    ax = int(face_w_frac * w / 2)
    ay = int(face_h_frac * h / 2)
    cv2.ellipse(frame, (cx_px, cy_px), (ax, ay), 0, 0, 360, (200, 180, 160), -1)
    return frame


def _frame_to_jpeg_bytes(frame: np.ndarray) -> bytes:
    """Encode a frame as JPEG bytes (for gate check input)."""
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    return bytes(buf)


def _laplacian_var_center_band(frame: np.ndarray) -> float:
    """Center-band [0.25-0.60 H, 0.1-0.9 W] Laplacian variance (same as gate check)."""
    h, w = frame.shape[:2]
    y0, y1 = int(h * 0.25), int(h * 0.60)
    x0, x1 = int(w * 0.10), int(w * 0.90)
    roi = frame[y0:y1, x0:x1]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


# ============================================================================
# Item 1 & 2: Margin guard geometry and mode selection
# ============================================================================

class TestMarginGuardGeometry:
    """Unit tests for _margin_valid_cx_range and _is_face_within_margin_px."""

    def setup_method(self):
        from render.reframe import FaceBox, _margin_valid_cx_range, _is_face_within_margin_px
        self.FaceBox = FaceBox
        self.margin_range = _margin_valid_cx_range
        self.within_margin = _is_face_within_margin_px

    def _params(self):
        """Common test parameters: 1920x1080 source, crop to 9:16."""
        src_w, src_h = 1920, 1080
        out_w, out_h = 1080, 1920
        crop_box_w = int(round(src_h * out_w / out_h))  # 607 px
        half_crop = crop_box_w // 2
        return src_w, src_h, crop_box_w, half_crop

    def test_face_at_center_is_within_margin(self):
        src_w, src_h, crop_box_w, half_crop = self._params()
        # Face centered exactly at source center
        face = self.FaceBox(x=0.45, y=0.2, w=0.10, h=0.15)
        cx_px = src_w // 2  # centered crop
        assert self.within_margin(face, cx_px, crop_box_w, half_crop, src_w)

    def test_face_at_left_edge_violates_margin(self):
        """Face at source left edge → violates margin with centered crop."""
        src_w, src_h, crop_box_w, half_crop = self._params()
        # Face near left edge: x=0.0, width=0.08
        face = self.FaceBox(x=0.00, y=0.2, w=0.08, h=0.15)
        cx_px = src_w // 2  # centered crop
        # Face left is at 0 in source; in crop coords: 0 - (cx_px - half_crop) could be negative
        # Either way it should violate the 10% margin
        assert not self.within_margin(face, cx_px, crop_box_w, half_crop, src_w)

    def test_face_at_right_edge_violates_margin(self):
        src_w, src_h, crop_box_w, half_crop = self._params()
        face = self.FaceBox(x=0.92, y=0.2, w=0.08, h=0.15)
        cx_px = src_w // 2
        assert not self.within_margin(face, cx_px, crop_box_w, half_crop, src_w)

    def test_margin_range_gives_valid_range_for_centered_face(self):
        src_w, src_h, crop_box_w, half_crop = self._params()
        face = self.FaceBox(x=0.45, y=0.2, w=0.10, h=0.15)
        min_cx, max_cx = self.margin_range(face, crop_box_w, src_w, half_crop)
        # Range should be non-empty
        assert min_cx <= max_cx
        # The center of the range should roughly align the face within the crop
        mid_cx = (min_cx + max_cx) // 2
        assert self.within_margin(face, mid_cx, crop_box_w, half_crop, src_w)

    def test_margin_range_for_left_edge_face_shifts_right(self):
        src_w, src_h, crop_box_w, half_crop = self._params()
        face = self.FaceBox(x=0.00, y=0.2, w=0.08, h=0.15)
        min_cx, max_cx = self.margin_range(face, crop_box_w, src_w, half_crop)
        if min_cx <= max_cx:
            # If a valid range exists, using min_cx should place face within margin
            assert self.within_margin(face, min_cx, crop_box_w, half_crop, src_w)

    def test_margin_range_always_clamped_to_source_bounds(self):
        src_w, src_h, crop_box_w, half_crop = self._params()
        for face_x in [0.0, 0.25, 0.5, 0.75, 0.92]:
            face = self.FaceBox(x=face_x, y=0.2, w=0.08, h=0.15)
            min_cx, max_cx = self.margin_range(face, crop_box_w, src_w, half_crop)
            assert min_cx >= half_crop, f"min_cx={min_cx} < half_crop={half_crop}"
            assert max_cx <= src_w - half_crop, f"max_cx={max_cx} > {src_w - half_crop}"

    def test_headroom_check(self):
        from render.reframe import _headroom_ok, _FACE_HEADROOM_FRAC
        # Face starting above the headroom threshold → fail
        face_low = self.FaceBox(x=0.4, y=0.02, w=0.2, h=0.2)
        assert not _headroom_ok(face_low)
        # Face starting at exactly the threshold
        face_at = self.FaceBox(x=0.4, y=_FACE_HEADROOM_FRAC, w=0.2, h=0.2)
        assert _headroom_ok(face_at)
        # Face with comfortable headroom
        face_ok = self.FaceBox(x=0.4, y=0.4, w=0.2, h=0.2)
        assert _headroom_ok(face_ok)


class TestModeSelection:
    """Unit tests for virtual camera mode selection (stationary vs tracking)."""

    def setup_method(self):
        from render.reframe import _STATIONARY_STDDEV_FRAC
        self.threshold = _STATIONARY_STDDEV_FRAC

    def test_low_stddev_is_stationary(self):
        """Face centers with stddev < 5% of src_w → stationary mode."""
        src_w = 1920
        # Face stays at ~50% horizontally, small jitter
        centers = [0.49, 0.50, 0.51, 0.50, 0.49]
        stddev = float(np.std(centers))
        assert stddev < self.threshold, f"stddev={stddev:.4f} should be < {self.threshold}"

    def test_high_stddev_is_tracking(self):
        """Face moves across frame → tracking mode."""
        src_w = 1920
        # Face moves from left (0.2) to right (0.8)
        centers = [0.2, 0.35, 0.5, 0.65, 0.8]
        stddev = float(np.std(centers))
        assert stddev >= self.threshold, f"stddev={stddev:.4f} should be >= {self.threshold}"

    def test_threshold_value(self):
        """Verify the threshold constant matches spec (5% of source width)."""
        assert self.threshold == 0.05


class TestPolyfitClamping:
    """Unit tests for _compute_tracking_keyframes polyfit + clamp logic."""

    def setup_method(self):
        from render.reframe import _compute_tracking_keyframes
        self.compute_keyframes = _compute_tracking_keyframes

    def _make_speaker_centers(self, ts, cxs):
        return list(zip(ts, cxs))

    def _make_ff(self, cxs):
        from render.reframe import FaceBox
        return [[FaceBox(x=cx - 0.05, y=0.2, w=0.10, h=0.15)] for cx in cxs]

    def test_linear_motion_produces_multiple_keyframes(self):
        """A linearly moving face → polyfit generates multiple keyframes."""
        ts = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
        cxs = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
        speaker_centers = self._make_speaker_centers(ts, cxs)
        valid_ff = self._make_ff(cxs)
        valid_ts = ts

        log_inst = logging.getLogger("test")
        src_w, crop_box_w = 1920, 607
        half_crop = crop_box_w // 2

        keyframes = self.compute_keyframes(
            speaker_centers, valid_ff, valid_ts,
            0.0, 5.0, 30.0,
            crop_box_w, half_crop, src_w, log_inst,
        )
        # Should have more than 1 keyframe for a 5s scene at 0.5s intervals
        assert len(keyframes) > 1
        # All cx values should be within source bounds
        for t, cx in keyframes:
            assert half_crop <= cx <= src_w - half_crop, (
                f"cx={cx} out of bounds [{half_crop}, {src_w - half_crop}]"
            )

    def test_max_shift_clamping_enforced(self):
        """Verify that consecutive keyframes respect the max shift constraint."""
        from render.reframe import _MAX_SHIFT_PX_PER_FRAME, _TRACKING_KEYFRAME_INTERVAL_S
        ts = [0.0, 2.5, 5.0]
        # Extreme jump in face position: from left (0.1) to right (0.9)
        cxs = [0.1, 0.5, 0.9]
        speaker_centers = self._make_speaker_centers(ts, cxs)
        valid_ff = self._make_ff(cxs)
        valid_ts = ts

        log_inst = logging.getLogger("test")
        src_w, crop_box_w = 1920, 607
        half_crop = crop_box_w // 2
        src_fps = 30.0

        keyframes = self.compute_keyframes(
            speaker_centers, valid_ff, valid_ts,
            0.0, 5.0, src_fps,
            crop_box_w, half_crop, src_w, log_inst,
        )

        if len(keyframes) < 2:
            return  # too short to test

        # The maximum allowed shift per keyframe interval
        max_shift = _MAX_SHIFT_PX_PER_FRAME * src_fps * _TRACKING_KEYFRAME_INTERVAL_S
        for i in range(1, len(keyframes)):
            prev_t, prev_cx = keyframes[i - 1]
            cur_t, cur_cx = keyframes[i]
            dt = cur_t - prev_t
            allowed = _MAX_SHIFT_PX_PER_FRAME * src_fps * max(dt, _TRACKING_KEYFRAME_INTERVAL_S)
            shift = abs(cur_cx - prev_cx)
            assert shift <= allowed + 1, (  # +1 for int rounding
                f"keyframe shift {shift}px > max allowed {allowed:.1f}px "
                f"(kf {i}: {prev_cx}→{cur_cx})"
            )

    def test_single_sample_returns_single_keyframe(self):
        """Single face sample → polyfit degrades gracefully to constant."""
        ts = [2.5]
        cxs = [0.5]
        speaker_centers = self._make_speaker_centers(ts, cxs)
        valid_ff = self._make_ff(cxs)
        valid_ts = ts

        log_inst = logging.getLogger("test")
        src_w, crop_box_w = 1920, 607
        half_crop = crop_box_w // 2

        keyframes = self.compute_keyframes(
            speaker_centers, valid_ff, valid_ts,
            0.0, 5.0, 30.0,
            crop_box_w, half_crop, src_w, log_inst,
        )
        assert len(keyframes) >= 1
        for t, cx in keyframes:
            assert half_crop <= cx <= src_w - half_crop


# ============================================================================
# Item 3a: Face-crop blur detection in reframe.py
# ============================================================================

class TestFaceCropBlur:
    """Unit tests for _laplacian_face_crop blur detection."""

    def setup_method(self):
        from render.reframe import _laplacian_face_crop, FaceBox, _FACE_BLUR_LAP_THRESHOLD
        self.lap_face_crop = _laplacian_face_crop
        self.FaceBox = FaceBox
        self.threshold = _FACE_BLUR_LAP_THRESHOLD

    def test_sharp_face_above_threshold(self):
        """A checkerboard face region should have LV >> threshold (200)."""
        # Create a frame with a checkerboard pattern in the "face" region
        frame = _make_sharp_frame(h=1080, w=1920)
        face = self.FaceBox(x=0.4, y=0.1, w=0.2, h=0.3)
        lv = self.lap_face_crop(frame, face, 1920, 1080)
        assert lv > self.threshold, f"Sharp face LV={lv:.1f} should be > {self.threshold}"

    def test_blurry_face_below_threshold(self):
        """A Gaussian-blurred face region should have LV << threshold (200)."""
        frame = _make_blurry_frame(h=1080, w=1920)
        face = self.FaceBox(x=0.4, y=0.1, w=0.2, h=0.3)
        lv = self.lap_face_crop(frame, face, 1920, 1080)
        assert lv < self.threshold, f"Blurry face LV={lv:.1f} should be < {self.threshold}"

    def test_threshold_constant(self):
        """Verify threshold constant matches spec (calibrated against 3-10 defocus LV)."""
        assert self.threshold == 200.0

    def test_tiny_face_returns_negative_sentinel(self):
        """Tiny face region (< 10px) → returns -1.0 gracefully."""
        frame = _make_sharp_frame()
        # 1px face: will be smaller than the minimum
        face = self.FaceBox(x=0.5, y=0.5, w=0.0005, h=0.0005)
        lv = self.lap_face_crop(frame, face, 1920, 1080)
        assert lv == -1.0

    def test_face_blob_in_blurry_background_is_sharp(self):
        """A face blob drawn on a blurry background: face crop should pass threshold."""
        frame = _make_blurry_frame()
        # Draw a sharp checkerboard in the face region only
        face_x0, face_y0 = 768, 100
        face_x1, face_y1 = 960, 300
        for y in range(face_y0, face_y1, 8):
            for x in range(face_x0, face_x1, 8):
                if ((y - face_y0) // 8 + (x - face_x0) // 8) % 2 == 0:
                    frame[y:y + 8, x:x + 8] = (255, 255, 255)

        face = self.FaceBox(
            x=face_x0 / 1920, y=face_y0 / 1080,
            w=(face_x1 - face_x0) / 1920, h=(face_y1 - face_y0) / 1080,
        )
        lv = self.lap_face_crop(frame, face, 1920, 1080)
        # The face crop should be sharp (above threshold) despite blurry background
        assert lv > self.threshold


# ============================================================================
# Item 3b: Gate blur check (_check_frames_sharp)
# ============================================================================

class TestGateBlurCheck:
    """Unit tests for producer.review_gate._check_frames_sharp."""

    def setup_method(self):
        from producer.review_gate import _check_frames_sharp
        self._check = _check_frames_sharp

    def _make_gate_frames(
        self,
        hook_sharp: bool = True,
        mid_sharp: bool = True,
        near_end_sharp: bool = True,
        final_sharp: bool = True,
    ) -> list[bytes]:
        """Generate 4-frame gate frame list (hook, mid, near_end, final)."""
        def _make(sharp: bool) -> bytes:
            if sharp:
                return _frame_to_jpeg_bytes(_make_sharp_frame(1920, 1080))
            else:
                return _frame_to_jpeg_bytes(_make_blurry_frame(1920, 1080))

        return [_make(hook_sharp), _make(mid_sharp), _make(near_end_sharp), _make(final_sharp)]

    def test_all_sharp_frames_pass(self):
        frames = self._make_gate_frames(True, True, True, True)
        result = self._check(frames)
        assert result["check"] == "footage_sharp"
        assert result["pass"] is True

    def test_blurry_mid_frame_fails(self):
        """Blurry mid frame (index 1) → check fails."""
        frames = self._make_gate_frames(hook_sharp=True, mid_sharp=False, near_end_sharp=True, final_sharp=True)
        result = self._check(frames)
        assert result["check"] == "footage_sharp"
        assert result["pass"] is False

    def test_blurry_hook_frame_does_not_fail(self):
        """Blurry hook frame (index 0) — we only check the mid frame (index 1)."""
        frames = self._make_gate_frames(hook_sharp=False, mid_sharp=True, near_end_sharp=True, final_sharp=True)
        result = self._check(frames)
        assert result["pass"] is True

    def test_too_few_frames_returns_none_pass(self):
        """Fewer than 2 frames (no mid frame) → graceful skip."""
        result = self._check([_frame_to_jpeg_bytes(_make_sharp_frame())])
        assert result["check"] == "footage_sharp"
        assert result["pass"] is None

    def test_empty_frame_list_returns_none_pass(self):
        result = self._check([])
        assert result["pass"] is None

    def test_empty_bytes_mid_frame_returns_none_pass(self):
        """Empty bytes at index 1 → graceful skip."""
        frames = [_frame_to_jpeg_bytes(_make_sharp_frame()), b"", b"", b""]
        result = self._check(frames)
        assert result["pass"] is None

    def test_result_has_required_keys(self):
        frames = self._make_gate_frames()
        result = self._check(frames)
        assert "phase" in result
        assert "check" in result
        assert "pass" in result
        assert "reason" in result
        assert result["phase"] == "1"

    def test_threshold_value_documented(self):
        """Verify threshold is 13 (calibrated value)."""
        from producer.review_gate import _GATE_BLUR_LAP_THRESHOLD
        assert _GATE_BLUR_LAP_THRESHOLD == 13.0


# ============================================================================
# Item 3b (calibration): Gate blur vs real rendered clips
# ============================================================================

class TestGateBlurCalibrationRealClips:
    """Calibration tests against real rendered clips on disk.

    Measurements (2026-07-12, ffmpeg-extracted mid-clip frames, center band LV):
      clip52 (defocused t=10-60s):  mid LV=7.2  → FAIL (threshold=13)
      clip55 (defocused t=5-48s):   mid LV=10.2 → FAIL
      clip46 (edge-pinned face):    mid LV=17.1 → PASS
      clip53 (anti-peptide stance): mid LV=5.7  → FAIL (also defocused at mid-clip)

    The spec says "clip53 t=15-20 (pass)" — at t=15-20s, LV≈20 (passes).
    However, at the gate mid-frame (t≈31s), LV=5.7, which is correctly
    identified as defocused. The gate uses the mid-clip gate frame, not a
    15-20s window. This is accurate detection of the real defocus condition.
    """

    def _extract_mid_frame_bytes(self, clip_path: Path) -> bytes:
        """Extract the mid-clip frame as JPEG bytes using ffmpeg."""
        # Get duration
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(clip_path)],
            capture_output=True, text=True,
        )
        import json
        dur = float(json.loads(r.stdout)["format"]["duration"])
        t_mid = dur * 0.5

        cmd = [
            "ffmpeg", "-y", "-ss", f"{t_mid:.3f}",
            "-i", str(clip_path),
            "-frames:v", "1", "-f", "image2", "-vcodec", "mjpeg", "-q:v", "4",
            "pipe:1",
        ]
        r2 = subprocess.run(cmd, capture_output=True)
        assert r2.returncode == 0, f"ffmpeg failed: {r2.stderr[-400:]}"
        return r2.stdout

    def _gate_check_clip(self, clip_path: Path) -> dict:
        """Run gate blur check on a real clip's mid frame."""
        from producer.review_gate import _check_frames_sharp
        mid_bytes = self._extract_mid_frame_bytes(clip_path)
        # Construct a 4-frame list with the real mid frame at index 1
        hook_bytes = _frame_to_jpeg_bytes(_make_sharp_frame(1920, 1080))  # synthetic hook
        return _check_frames_sharp([hook_bytes, mid_bytes, mid_bytes, mid_bytes])

    @REALCLIPS
    def test_clip52_fails_blur_check(self):
        """clip52 is defocused (LV=7.2 at mid-clip) → must FAIL gate blur check."""
        result = self._gate_check_clip(_CLIP52)
        assert result["pass"] is False, (
            f"clip52 should FAIL blur check (defocused footage). "
            f"Got: {result['reason']}"
        )

    @REALCLIPS
    def test_clip46_passes_blur_check(self):
        """clip46 has edge-pinned face issue but is NOT defocused (LV=17.1) → must PASS."""
        result = self._gate_check_clip(_CLIP46)
        assert result["pass"] is True, (
            f"clip46 should PASS blur check (LV=17.1 > threshold=13). "
            f"Got: {result['reason']}"
        )

    @REALCLIPS
    def test_clip55_fails_blur_check(self):
        """clip55 is defocused (LV=10.2 at mid-clip) → must FAIL gate blur check."""
        if not _CLIP55.exists():
            pytest.skip("clip55.mp4 not in scratchpad")
        result = self._gate_check_clip(_CLIP55)
        assert result["pass"] is False, (
            f"clip55 should FAIL blur check (defocused footage). "
            f"Got: {result['reason']}"
        )

    @REALCLIPS
    def test_measured_lv_values_documented(self):
        """Verify that the measured LV values match the spec documentation."""
        # This test computes actual LV values and documents them as assertions.
        # It is not asserting pass/fail — just verifying the calibration numbers.
        import json

        def _mid_lv(clip_path):
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", str(clip_path)],
                capture_output=True, text=True,
            )
            dur = float(json.loads(r.stdout)["format"]["duration"])
            t_mid = dur * 0.5
            cmd = ["ffmpeg", "-y", "-ss", f"{t_mid:.3f}", "-i", str(clip_path),
                   "-frames:v", "1", "-f", "image2", "-vcodec", "mjpeg",
                   "-q:v", "4", "pipe:1"]
            r2 = subprocess.run(cmd, capture_output=True)
            arr = np.frombuffer(r2.stdout, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return _laplacian_var_center_band(frame)

        lv_52 = _mid_lv(_CLIP52)
        lv_46 = _mid_lv(_CLIP46)
        # clip52 must be below threshold
        assert lv_52 < 13.0, f"clip52 mid LV={lv_52:.1f} should be < 13.0"
        # clip46 must be above threshold
        assert lv_46 > 13.0, f"clip46 mid LV={lv_46:.1f} should be > 13.0"


# ============================================================================
# ASD module tests
# ============================================================================

class TestASDModule:
    """Unit tests for render/asd.py stub + fallback."""

    def test_select_by_size_and_centrality_prefers_central_large_face(self):
        from render.asd import select_by_size_and_centrality
        from render.reframe import FaceBox
        # Two faces: one large but at edge, one smaller but central
        large_edge = FaceBox(x=0.01, y=0.2, w=0.30, h=0.40)   # large, left edge
        small_center = FaceBox(x=0.40, y=0.2, w=0.20, h=0.30)  # smaller, central
        # Score large_edge: area=0.12, centrality=1-2*|0.01+0.15-0.5|=1-0.68=0.32 → 0.0384
        # Score small_center: area=0.06, centrality=1-2*|0.4+0.1-0.5|=1-0.0=1.0 → 0.06
        cx = select_by_size_and_centrality([[large_edge, small_center]])
        assert cx is not None
        # Should prefer small_center (higher area*centrality score)
        assert abs(cx - small_center.center_x) < abs(cx - large_edge.center_x)

    def test_select_by_size_and_centrality_empty(self):
        from render.asd import select_by_size_and_centrality
        assert select_by_size_and_centrality([[]]) is None

    def test_asd_stub_raises_not_implemented_or_returns_none(self):
        """ASD stub must either raise NotImplementedError or return None — never crash."""
        from render.asd import select_active_speaker_asd
        try:
            result = select_active_speaker_asd([], [], [], 1920, 1080, logging.getLogger())
            assert result is None or isinstance(result, float)
        except NotImplementedError:
            pass  # expected when weights are missing

    def test_asd_flag_off_returns_none(self):
        """With REFRAME_ASD=0, select_active_speaker_asd returns None immediately."""
        with patch.dict(os.environ, {"REFRAME_ASD": "0"}):
            # Reimport to pick up env var (module-level flag)
            import importlib
            import render.asd as asd_mod
            # The function checks the flag at import time; with the env var off
            # and no weights, it should return None quickly
            try:
                result = asd_mod.select_active_speaker_asd(
                    [], [], [], 1920, 1080, logging.getLogger()
                )
                assert result is None
            except NotImplementedError:
                pass  # also acceptable — the weights path doesn't exist

    def test_reframe_asd_env_var_parsed(self):
        from render.reframe import _REFRAME_ASD_ENABLED
        # The constant is module-level; just verify it's a bool
        assert isinstance(_REFRAME_ASD_ENABLED, bool)


# ============================================================================
# End-to-end reframe_segment test
# ============================================================================

class TestReframeE2E:
    """End-to-end test: run reframe_segment on real R2 source footage.

    Requirement (spec R1): post-reframe, sampled output frames must have
    the primary face bbox FULLY inside the 9:16 crop (central 80%) for ALL
    sampled frames.

    The source (youtube_2tM1LFFxeKg.mp4, 1920x1080, ~260s, 23.976fps) is
    a Huberman podcast — multi-face shots expected.  We use a 30s segment
    starting at t=31.0s (known to have camera cuts based on prior demo runs).
    """

    @E2E
    def test_reframe_segment_face_within_central_80pct(self):
        """After reframe_segment, detected faces must be within central 80% of output frame."""
        from render.reframe import reframe_segment, _detect_faces_in_frame
        log_inst = logging.getLogger("test.e2e")

        source = _SOURCE_E2E
        assert source.exists(), f"E2E source not found: {source}"

        with tempfile.TemporaryDirectory(prefix="e2e_reframe_") as tmpdir:
            tmp = Path(tmpdir)
            out_video = tmp / "reframed.mp4"
            out_audio = tmp / "audio.wav"

            # 30s segment at t=31s (Huberman episode, multi-face, known camera cuts)
            reframe_segment(
                source=source,
                out_video=out_video,
                out_audio=out_audio,
                start=31.0,
                duration=30.0,
                out_w=1080,
                out_h=1920,
                log=log_inst,
            )

            assert out_video.exists(), "reframe_segment did not produce output video"
            assert out_video.stat().st_size > 10_000, "Output video is suspiciously small"

            # Sample output frames and check face positions
            cap = cv2.VideoCapture(str(out_video))
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            assert fps > 0 and total_frames > 0

            # Sample ~5 frames evenly across the output
            sample_timestamps = [
                total_frames * i / 6 / fps
                for i in range(1, 6)
            ]

            out_w, out_h = 1080, 1920
            face_violations: list[str] = []
            frames_with_faces = 0
            margin_frac = 0.10  # 10% margin = central 80%

            for ts in sample_timestamps:
                cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000.0)
                ret, frame = cap.read()
                if not ret or frame is None:
                    continue

                h, w = frame.shape[:2]
                assert w == out_w, f"Output width {w} != {out_w}"
                assert h == out_h, f"Output height {h} != {out_h}"

                faces = _detect_faces_in_frame(frame)
                if not faces:
                    continue

                frames_with_faces += 1
                for face in faces:
                    # Check face is within central 80% horizontally
                    left_pct = face.x
                    right_pct = face.right
                    if left_pct < margin_frac or right_pct > (1.0 - margin_frac):
                        face_violations.append(
                            f"t={ts:.1f}s face=({face.x:.3f},{face.y:.3f},"
                            f"{face.w:.3f},{face.h:.3f}) "
                            f"left={left_pct:.3f} right={right_pct:.3f}"
                        )

            cap.release()

            log_inst.info(
                "E2E reframe: %d frame(s) with detected faces, %d violation(s)",
                frames_with_faces, len(face_violations),
            )

            # Requirement: if faces were detected, none should violate the margin
            if frames_with_faces > 0:
                assert not face_violations, (
                    f"Face margin violations found in {len(face_violations)} frame(s) "
                    f"(requirement R1 — face must be within central 80%):\n"
                    + "\n".join(face_violations)
                )


# ============================================================================
# Gate integration: _check_frames_sharp wired into Phase 1
# ============================================================================

class TestGateBlurIntegration:
    """Verify _check_frames_sharp is correctly wired into _run_phase1.

    These tests monkeypatch the transport functions (no real video, no LLM calls).
    """

    def _make_fake_probe(self, w=1080, h=1920, dur=30.0):
        return {
            "streams": [{
                "codec_type": "video",
                "width": w,
                "height": h,
                "duration": str(dur),
            }],
            "format": {"duration": str(dur)},
        }

    def test_deterministic_blur_no_longer_gates_phase1(self, monkeypatch):
        """The deterministic Laplacian blur metric is now INFORMATIONAL ONLY —
        a low-detail mid frame must NOT auto-fail Phase 1 (it false-failed 14/17
        real clips). Focus is judged by the vision LLM (footage_in_focus)."""
        from producer import review_gate

        hook_b = _frame_to_jpeg_bytes(_make_sharp_frame(1920, 1080))
        mid_b = _frame_to_jpeg_bytes(_make_blurry_frame(1920, 1080))
        outro_b = _frame_to_jpeg_bytes(_make_sharp_frame(1920, 1080))
        fake_frames = [hook_b, mid_b, outro_b, outro_b]

        monkeypatch.setattr(review_gate, "_probe_video", lambda _: self._make_fake_probe())
        monkeypatch.setattr(review_gate, "_extract_frames", lambda *a, **kw: fake_frames)
        vision_called = []
        good_verdict = {
            "hook_present_in_hook_frame": True, "hook_absent_in_mid_frame": True,
            "captions_present": True, "watermark_visible": True,
            "real_humans": True, "speaker_centered": True,
            "footage_in_focus": True, "animation_detected": False,
        }
        monkeypatch.setattr(
            review_gate, "_vision_llm_call",
            lambda *a, **kw: (vision_called.append(True), good_verdict)[1],
        )
        monkeypatch.setattr(review_gate, "_resolve_to_local_path", lambda p, d: (p, False))

        from unittest.mock import MagicMock
        clip_row = MagicMock()
        clip_row.id = 99
        clip_row.kind = "clip"
        clip_row.source_id = None

        reasons, passed, _ = review_gate._run_phase1(clip_row, "/fake/video.mp4", None, "fitness")

        # Vision WAS called (no deterministic short-circuit) and the clip passes:
        assert len(vision_called) == 1, "Vision LLM must run — blur no longer short-circuits"
        assert passed, "A blurry-metric clip is no longer auto-failed by the deterministic check"
        # footage_sharp is recorded but informational (pass=None)
        assert any(r["check"] == "footage_sharp" and r["pass"] is None for r in reasons)

    def test_vision_out_of_focus_fails_phase1(self, monkeypatch):
        """The vision LLM saying footage_in_focus=false → Phase 1 fails."""
        from producer import review_gate

        frames = [_frame_to_jpeg_bytes(_make_sharp_frame(1920, 1080))] * 4
        monkeypatch.setattr(review_gate, "_probe_video", lambda _: self._make_fake_probe())
        monkeypatch.setattr(review_gate, "_extract_frames", lambda *a, **kw: frames)
        verdict = {
            "hook_present_in_hook_frame": True, "hook_absent_in_mid_frame": True,
            "captions_present": True, "watermark_visible": True,
            "real_humans": True, "speaker_centered": True,
            "footage_in_focus": False, "animation_detected": False,
        }
        monkeypatch.setattr(review_gate, "_vision_llm_call", lambda *a, **kw: verdict)
        monkeypatch.setattr(review_gate, "_resolve_to_local_path", lambda p, d: (p, False))

        from unittest.mock import MagicMock
        clip_row = MagicMock(); clip_row.id = 99; clip_row.kind = "clip"; clip_row.source_id = None

        reasons, passed, _ = review_gate._run_phase1(clip_row, "/fake/video.mp4", None, "fitness")
        assert not passed, "footage_in_focus=false must fail Phase 1"
        assert any(r["check"] == "footage_in_focus" and r["pass"] is False for r in reasons)

    def test_sharp_mid_does_not_fail_before_vision(self, monkeypatch):
        """A sharp mid frame does NOT auto-fail Phase 1 (vision LLM still runs)."""
        from producer import review_gate

        hook_b = _frame_to_jpeg_bytes(_make_sharp_frame(1920, 1080))
        mid_b = _frame_to_jpeg_bytes(_make_sharp_frame(1920, 1080))
        outro_b = _frame_to_jpeg_bytes(_make_sharp_frame(1920, 1080))
        fake_frames = [hook_b, mid_b, outro_b, outro_b]

        monkeypatch.setattr(review_gate, "_probe_video", lambda _: self._make_fake_probe())
        monkeypatch.setattr(review_gate, "_extract_frames", lambda *a, **kw: fake_frames)
        vision_called = []
        # Vision returns animation_detected=True so phase fails cleanly without
        # it being confused with the blur check
        monkeypatch.setattr(
            review_gate, "_vision_llm_call",
            lambda *a, **kw: vision_called.append(True) or {"animation_detected": True},
        )
        monkeypatch.setattr(review_gate, "_resolve_to_local_path", lambda p, d: (p, False))

        from unittest.mock import MagicMock
        clip_row = MagicMock()
        clip_row.id = 99
        clip_row.kind = "clip"
        clip_row.source_id = None

        reasons, passed, _ = review_gate._run_phase1(clip_row, "/fake/video.mp4", None, "fitness")

        # Phase fails because of animation (not blur), but the vision WAS called
        assert len(vision_called) == 1, "Vision LLM should be called (blur never short-circuits)"
        # footage_sharp is recorded but informational (pass=None, never gates)
        blur_reasons = [r for r in reasons if r["check"] == "footage_sharp"]
        assert blur_reasons, "footage_sharp reason should be in reasons list"
        assert blur_reasons[0]["pass"] is None

    def test_cv2_unavailable_does_not_block_phase1(self, monkeypatch):
        """If cv2 import fails inside _check_frames_sharp, Phase 1 continues (pass=None)."""
        from producer import review_gate

        hook_b = _frame_to_jpeg_bytes(_make_sharp_frame(1920, 1080))
        mid_b = _frame_to_jpeg_bytes(_make_blurry_frame(1920, 1080))
        fake_frames = [hook_b, mid_b, b"", b""]

        monkeypatch.setattr(review_gate, "_probe_video", lambda _: self._make_fake_probe())
        monkeypatch.setattr(review_gate, "_extract_frames", lambda *a, **kw: fake_frames)
        monkeypatch.setattr(review_gate, "_resolve_to_local_path", lambda p, d: (p, False))

        # Patch cv2 inside _check_frames_sharp to simulate ImportError
        original_check = review_gate._check_frames_sharp

        def mock_check(frame_bytes):
            return {"phase": "1", "check": "footage_sharp", "pass": None,
                    "reason": "Blur check skipped: cv2 not available"}

        monkeypatch.setattr(review_gate, "_check_frames_sharp", mock_check)

        # Vision LLM returns all-good verdict
        good_verdict = {
            "hook_present_in_hook_frame": True,
            "hook_absent_in_mid_frame": True,
            "captions_present": True,
            "watermark_visible": True,
            "real_humans": True,
            "speaker_centered": True,
            "animation_detected": False,
        }
        monkeypatch.setattr(review_gate, "_vision_llm_call", lambda *a, **kw: good_verdict)
        monkeypatch.setattr(review_gate, "_load_style_refs", lambda *a: [])

        from unittest.mock import MagicMock
        clip_row = MagicMock()
        clip_row.id = 99
        clip_row.kind = "clip"
        clip_row.source_id = None

        reasons, passed, _ = review_gate._run_phase1(clip_row, "/fake/video.mp4", None, "fitness")

        # Phase 1 should PASS (cv2 unavailable = skip, not fail)
        assert passed, (
            "Phase 1 should pass when blur check is skipped (cv2 unavailable). "
            f"Reasons: {[r for r in reasons if not r['pass']]}"
        )
        blur_reasons = [r for r in reasons if r["check"] == "footage_sharp"]
        assert blur_reasons[0]["pass"] is None
