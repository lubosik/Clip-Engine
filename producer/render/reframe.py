"""
reframe.py — Face/subject-aware 16:9 → 9:16 reframing.

Strategy
--------
1.  Probe source for (W, H, duration) via ffprobe.
2.  Sample frames at ~2 fps using ffmpeg → PNG files in a temp subdir.
3.  For each sample frame, detect the dominant face/subject horizontal centre:
      a. mediapipe face detection  (guarded import — preferred)
      b. OpenCV Haar cascade        (guarded import — fallback)
      c. horizontal centre of frame (final fallback)
4.  Apply exponential moving average (alpha=0.15) to smooth crop_x.
    Hard-cut detection: if the RAW detected centre jumps > 35% of source width
    between consecutive samples, reset the EMA to the new position immediately
    (produces a hard cut in output rather than a slow pan across the frame).
5.  Build a piecewise-constant crop_x expression for ffmpeg's `crop` filter
    using nested `if(lt(t,...),x,...)` calls — single encode pass, no concat.
6.  Run one ffmpeg pass:  crop → scale → libx264 veryfast crf 20.

Crop maths (16:9 source → 9:16 output)
---------------------------------------
    source: W × H  (e.g. 1280 × 720)
    target: out_w × out_h  (e.g. 1080 × 1920)

    target_aspect = out_w / out_h  = 9/16 = 0.5625

    If source_aspect >= target_aspect  (landscape / square):
        crop_h = source_H
        crop_w = round(source_H * out_w / out_h)
        crop_y = 0
        crop_x = clamp(face_cx - crop_w/2, 0, source_W - crop_w)

    If source_aspect < target_aspect  (portrait narrower than 9:16):
        crop_w = source_W
        crop_h = round(source_W * out_h / out_w)
        crop_x = 0
        crop_y = clamp(face_cy - crop_h/2, 0, source_H - crop_h)

    Then scale crop_w × crop_h → out_w × out_h.

Audio is copied unchanged (no re-encode needed).
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# EMA smoothing factor: smaller = smoother but slower to follow movement
_EMA_ALPHA = 0.15

# Hard-cut threshold: fraction of source width
_HARD_CUT_THRESHOLD = 0.35

# Frame sampling rate for face detection
_SAMPLE_FPS = 2.0


def reframe_clip(
    cut_video: Path,
    resolution: list[int],
    workdir: Path,
    *,
    stem: str = "reframed",
) -> Path:
    """Reframe *cut_video* from any aspect ratio to 9:16 at *resolution*.

    Parameters
    ----------
    cut_video   : already-cut MP4 (starts at t=0).
    resolution  : [out_w, out_h] e.g. [1080, 1920].
    workdir     : temp directory for sample frames.
    stem        : output filename stem.

    Returns
    -------
    Path to the reframed MP4 (libx264 veryfast crf 20, audio copy).
    """
    out_w, out_h = resolution
    output_path = workdir / f"{stem}.mp4"

    # ------------------------------------------------------------------
    # Probe source
    # ------------------------------------------------------------------
    info = _probe_video(cut_video)
    src_w = info["width"]
    src_h = info["height"]
    duration = info["duration"]

    # ------------------------------------------------------------------
    # Compute static crop dimensions
    # ------------------------------------------------------------------
    target_aspect = out_w / out_h  # 9/16
    src_aspect = src_w / src_h

    if src_aspect >= target_aspect:
        # Landscape/square: crop width, keep full height
        crop_w = round(src_h * out_w / out_h)
        crop_h = src_h
        crop_axis = "x"           # tracking varies crop_x
        crop_fixed = 0            # crop_y = 0
        center_default = src_w / 2
        clamp_min = 0
        clamp_max = src_w - crop_w
        half_crop = crop_w / 2
    else:
        # Portrait narrower than target: crop height, keep full width
        crop_w = src_w
        crop_h = round(src_w * out_h / out_w)
        crop_axis = "y"           # tracking varies crop_y
        crop_fixed = 0            # crop_x = 0
        center_default = src_h / 2
        clamp_min = 0
        clamp_max = src_h - crop_h
        half_crop = crop_h / 2

    if clamp_max < 0:
        # Source is smaller than crop region in one dimension — just scale
        log.warning(
            "reframe_clip: source %dx%d is too small for crop %dx%d; "
            "falling back to pad+scale.",
            src_w, src_h, crop_w, crop_h,
        )
        return _scale_with_pad(cut_video, out_w, out_h, output_path)

    # ------------------------------------------------------------------
    # Sample frames and detect face centres
    # ------------------------------------------------------------------
    frames_dir = workdir / "sample_frames"
    frames_dir.mkdir(exist_ok=True)

    _extract_sample_frames(cut_video, frames_dir, fps=_SAMPLE_FPS)

    frame_paths = sorted(frames_dir.glob("frame_*.png"))
    if not frame_paths:
        log.warning("reframe_clip: no sample frames extracted; using centre crop.")
        frame_paths = []

    # Map each frame index → time in the clip
    sample_interval = 1.0 / _SAMPLE_FPS
    sample_times = [i * sample_interval for i in range(len(frame_paths))]

    # Detect face/subject centre for each sample frame
    detector = _build_detector()
    raw_centers: list[float] = []
    for fp in frame_paths:
        cx = detector(fp, src_w, src_h, crop_axis)
        raw_centers.append(cx if cx is not None else center_default)

    if not raw_centers:
        raw_centers = [center_default]
        sample_times = [0.0]

    # ------------------------------------------------------------------
    # EMA smoothing + hard-cut detection
    # ------------------------------------------------------------------
    # A segment is a contiguous span where no hard cut occurred.
    # Each segment carries the mean EMA crop position for that span.
    segments: list[dict] = []
    ema_val = raw_centers[0]
    seg_start_t = 0.0
    seg_ema_vals: list[float] = [ema_val]
    prev_raw = raw_centers[0]

    for i in range(1, len(raw_centers)):
        raw = raw_centers[i]
        jump = abs(raw - prev_raw)

        if jump > _HARD_CUT_THRESHOLD * (src_w if crop_axis == "x" else src_h):
            # Hard cut: close current segment, reset EMA
            seg_end_t = sample_times[i]
            segments.append({
                "start": seg_start_t,
                "end": seg_end_t,
                "crop_pos": _clamp(_mean(seg_ema_vals) - half_crop, clamp_min, clamp_max),
            })
            seg_start_t = seg_end_t
            ema_val = raw
            seg_ema_vals = [ema_val]
        else:
            ema_val = _EMA_ALPHA * raw + (1 - _EMA_ALPHA) * ema_val
            seg_ema_vals.append(ema_val)

        prev_raw = raw

    # Close final segment (runs to clip end)
    segments.append({
        "start": seg_start_t,
        "end": duration,
        "crop_pos": _clamp(_mean(seg_ema_vals) - half_crop, clamp_min, clamp_max),
    })

    # ------------------------------------------------------------------
    # Build ffmpeg crop position expression
    # ------------------------------------------------------------------
    crop_pos_expr = _build_piecewise_expr(segments)

    if crop_axis == "x":
        crop_filter = (
            f"crop={crop_w}:{crop_h}:{crop_pos_expr}:{crop_fixed},"
            f"scale={out_w}:{out_h}"
        )
    else:
        crop_filter = (
            f"crop={crop_w}:{crop_h}:{crop_fixed}:{crop_pos_expr},"
            f"scale={out_w}:{out_h}"
        )

    # ------------------------------------------------------------------
    # Single ffmpeg encode pass
    # ------------------------------------------------------------------
    cmd = [
        "ffmpeg", "-y",
        "-i", str(cut_video),
        "-vf", crop_filter,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"reframe_clip: ffmpeg failed (exit {result.returncode}):\n"
            f"FILTER: {crop_filter}\n"
            f"STDERR: {result.stderr[-3000:]}"
        )

    log.info(
        "reframe_clip: %s → %s  segments=%d  crop_axis=%s",
        cut_video.name, output_path.name, len(segments), crop_axis,
    )
    return output_path


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

def _extract_sample_frames(video: Path, out_dir: Path, fps: float) -> None:
    """Write sample PNG frames to *out_dir* at *fps* frames per second."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        "-vf", f"fps={fps}",
        "-q:v", "3",
        str(out_dir / "frame_%04d.png"),
    ]
    subprocess.run(cmd, capture_output=True)  # ignore errors; empty dir is handled


# ---------------------------------------------------------------------------
# Face / subject centre detection
# ---------------------------------------------------------------------------

def _build_detector():
    """Return a callable detector(frame_path, src_w, src_h, axis) → float | None.

    Tries mediapipe first, then OpenCV Haar cascade.  If neither is importable,
    returns a pass-through that always yields None (→ centre-crop fallback).
    """
    # ---- mediapipe -------------------------------------------------------
    try:
        import mediapipe as mp  # guarded heavy import
        import numpy as np
        from PIL import Image as PILImage

        _mp_face = mp.solutions.face_detection

        def detect_mediapipe(frame_path: Path, src_w: int, src_h: int, axis: str):
            try:
                img = PILImage.open(frame_path).convert("RGB")
                arr = np.array(img)
                with _mp_face.FaceDetection(
                    model_selection=0, min_detection_confidence=0.4
                ) as fd:
                    results = fd.process(arr)
                if not results.detections:
                    return None
                centers = []
                for det in results.detections:
                    bb = det.location_data.relative_bounding_box
                    if axis == "x":
                        cx = (bb.xmin + bb.width / 2) * src_w
                    else:
                        cx = (bb.ymin + bb.height / 2) * src_h
                    centers.append(cx)
                return sum(centers) / len(centers)
            except Exception as exc:
                log.debug("mediapipe detection error: %s", exc)
                return None

        log.info("reframe: using mediapipe face detector")
        return detect_mediapipe

    except ImportError:
        pass

    # ---- OpenCV Haar cascade ---------------------------------------------
    try:
        import cv2  # guarded heavy import

        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        face_cascade = cv2.CascadeClassifier(cascade_path)

        def detect_cv2(frame_path: Path, src_w: int, src_h: int, axis: str):
            try:
                img = cv2.imread(str(frame_path))
                if img is None:
                    return None
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
                )
                if not len(faces):
                    return None
                centers = []
                for x, y, w, h in faces:
                    cx = (x + w / 2) if axis == "x" else (y + h / 2)
                    centers.append(cx)
                return sum(centers) / len(centers)
            except Exception as exc:
                log.debug("cv2 detection error: %s", exc)
                return None

        log.info("reframe: using OpenCV Haar cascade face detector")
        return detect_cv2

    except ImportError:
        pass

    # ---- no-op (centre crop fallback) ------------------------------------
    log.info("reframe: no face detector available; using centred-crop fallback")

    def detect_noop(frame_path: Path, src_w: int, src_h: int, axis: str):
        return None

    return detect_noop


# ---------------------------------------------------------------------------
# ffmpeg expression builder
# ---------------------------------------------------------------------------

def _build_piecewise_expr(segments: list[dict]) -> str:
    """Build a nested if(lt(t,...),x,...) expression for piecewise-constant crop.

    The last segment covers [t_N, ∞) so we only need N-1 boundary checks.
    """
    if len(segments) == 1:
        return str(int(round(segments[0]["crop_pos"])))

    # Build from right to left
    expr = str(int(round(segments[-1]["crop_pos"])))
    for seg in reversed(segments[:-1]):
        x = int(round(seg["crop_pos"]))
        t = seg["end"]
        expr = f"if(lt(t,{t:.4f}),{x},{expr})"
    return expr


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def _probe_video(path: Path) -> dict:
    """Return {width, height, duration} for the first video stream."""
    import json
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "v:0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr[:500]}")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(f"ffprobe: no video stream found in {path}")
    s = streams[0]
    return {
        "width": int(s["width"]),
        "height": int(s["height"]),
        "duration": float(s.get("duration", 0) or _probe_duration_fallback(path)),
    }


def _probe_duration_fallback(path: Path) -> float:
    """Use container duration when stream duration is missing."""
    import json
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout or "{}")
    return float(data.get("format", {}).get("duration", 0))


# ---------------------------------------------------------------------------
# Fallback: pad + scale when source is already narrower than target
# ---------------------------------------------------------------------------

def _scale_with_pad(src: Path, out_w: int, out_h: int, dst: Path) -> Path:
    """Scale and pad *src* to *out_w* x *out_h* with black bars."""
    vf = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"_scale_with_pad failed: {result.stderr[-2000:]}")
    return dst


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0
