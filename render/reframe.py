"""
render/reframe.py — Active-speaker-aware vertical reframing for 9:16 clips.

This module is imported by render/modal_app.py and replaces the static
center-crop path in _cut_and_reframe.  It is importable and runnable locally
on CPU without CUDA, OpenGL, or other GPU/display dependencies.

INTERFACE
---------
    reframe_segment(
        source: Path,
        out_video: Path,
        out_audio: Path,
        start: float,
        duration: float,
        out_w: int,
        out_h: int,
        log: logging.Logger,
    ) -> None

Pipeline
--------
1. Extract the raw [start, start+duration] segment from *source* using ffmpeg
   (re-encode at ultrafast/CRF-18 so all downstream seeks are accurate).
2. PySceneDetect AdaptiveDetector on the raw segment → list of (start_s, end_s)
   scene intervals in clip-relative time.
3. Per scene: sample frames at ~3 fps → face detection via OpenCV YuNet DNN.
4. Blur filtering: Laplacian variance on the face crop region per sampled frame.
   Blurry frames (_FACE_BLUR_LAP_THRESHOLD = 200, calibrated against measured
   defocus segments of 3-10 LV vs sharp studio >500) are excluded from
   anchoring and speaker selection.  Per-scene blur stats are logged.
5. Active-speaker selection:
   - 0 non-blurry faces in scene: center crop.
   - 1 non-blurry face: track it.
   - N>1 non-blurry faces: improved heuristic (largest+most-central face) or
     LR-ASD when REFRAME_ASD=1 (see render/asd.py).
6. Virtual camera mode per scene (AutoFlip pattern):
   - stationary: face-center stddev < _STATIONARY_STDDEV_FRAC (5%) of source
     width → constant crop (median center).
   - panning/tracking: 2nd-degree numpy.polyfit over (t, face_center_x) →
     smooth path at _TRACKING_KEYFRAME_INTERVAL_S (0.5s) keyframes, clamped
     to source bounds, max shift _MAX_SHIFT_PX_PER_FRAME (~1px/frame @30fps).
7. Face-margin guard + re-anchor (spec R1.1): for every sampled non-blurry
   frame, the active face bbox must sit FULLY within the central 80% of the
   crop width (10% margin each side) AND have _FACE_HEADROOM_FRAC (15%) from
   the top of the source frame.
   - Constant mode: intersect per-frame valid cx ranges.  If intersection is
     non-empty → use midpoint.  If empty → upgrade to tracking mode.
   - Tracking mode: clamp each keyframe cx to the valid range for that time.
   - All adjustments are clamped to source bounds.
   - Margin violations and corrections are logged (count per scene).
8. Apply via ffmpeg: per-scene (sub-)segment crops → temp files, then concat
   with the concat demuxer (-c copy; all segs same codec/size).
   Tracking mode generates one sub-segment per keyframe interval.
   Constant mode generates one segment per scene (existing shape).
9. Extract 16-kHz mono WAV from the reframed output to *out_audio*.
10. Global fallback: if no non-blurry faces detected in any scene (e.g.
    animated content), fall back to standard center crop for the entire clip.

Face detection
--------------
Primary: OpenCV FaceDetectorYN (YuNet DNN, ~228 KB ONNX model).
Model is loaded from the first path that exists in YUNET_MODEL_SEARCH_PATHS.
Falls back to Haar cascade if no model path resolves (no detection).

Active speaker upgrade path (LR-ASD)
--------------------------------------
When env var REFRAME_ASD=1 (default 0 / off locally), render/asd.py is
imported and LR-ASD (Junhua-Liao/LR-ASD, MIT) is attempted; any failure
falls back to the improved heuristic (largest+most-central face).  The fallback
chain ensures the pipeline never breaks for lack of the ASD model.

See render/asd.py for the stub + integration point.

MediaPipe / TalkNet-ASD (legacy upgrade path notes)
----------------------------------------------------
  A) MediaPipe FaceLandmarker (face mesh, lip landmark 13↔14 distance variance):
     Replace the heuristic with FaceLandmarker.  Requires libGLESv2.

  B) TalkNet-ASD (TaoRuijie/TalkNet-ASD — audio-visual active speaker):
     Slot in as ActiveSpeakerDetector.select() called from _analyze_scene.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FACE_SAMPLE_FPS: float = 3.0       # face-detection samples per second
_SMOOTH_SIGMA_SAMPLES: float = 2.0  # Gaussian σ in sample-frame units
_MOUTH_ROI_FRAC: float = 0.4        # lower fraction of face bbox used for mouth-variance

# Blur detection: Laplacian variance on the FACE CROP region (not full frame).
# Spec calibration: face-region LV in defocus segments = 3-10; sharp studio >500.
# Threshold 200 excludes clearly blurry frames while leaving room below sharp footage.
_FACE_BLUR_LAP_THRESHOLD: float = 200.0

# Face-margin guard (spec R1.1).
# The active face bbox must sit FULLY within the central 80% of the crop width
# (10% margin each side) and have 15% headroom from the top of the source frame.
_FACE_MARGIN_SIDE_FRAC: float = 0.10   # 10% margin each side within crop box
_FACE_HEADROOM_FRAC: float = 0.15      # 15% from top of source frame above face top edge

# Virtual camera mode selection (spec R1.2).
# stddev of face center_x as fraction of source width
_STATIONARY_STDDEV_FRAC: float = 0.05

# Tracking mode: max crop shift per frame at 30fps, and keyframe interval.
# Max shift: ~1px/frame; over 0.5s at 30fps = 15px max shift between keyframes.
_MAX_SHIFT_PX_PER_FRAME: float = 1.0
_TRACKING_KEYFRAME_INTERVAL_S: float = 0.5

# Feature flag: set REFRAME_ASD=1 in the environment to enable LR-ASD speaker detection.
# Requires render/asd.py to load successfully with model weights; falls back to the
# improved heuristic (largest+most-central face) on any import or weight error.
_REFRAME_ASD_ENABLED: bool = os.environ.get("REFRAME_ASD", "0") == "1"

# Paths searched for the YuNet ONNX model (first existing path wins).
# The repo ships the model at assets/models/face_detection_yunet.onnx.
# In the Modal container, it is also downloaded to /models/ at image-build time.
_REPO_MODEL_PATH = Path(__file__).parent.parent / "assets" / "models" / "face_detection_yunet.onnx"

YUNET_MODEL_SEARCH_PATHS: list[Path] = [
    _REPO_MODEL_PATH,
    Path("/models/face_detection_yunet.onnx"),  # Modal container
    Path(os.environ.get("REFRAME_YUNET_MODEL_PATH", "/dev/null")),
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FaceBox:
    """Face bounding box in normalised frame coordinates (0..1)."""
    x: float     # left edge
    y: float     # top edge
    w: float     # width
    h: float     # height

    @property
    def center_x(self) -> float:
        return self.x + self.w / 2.0

    @property
    def center_y(self) -> float:
        return self.y + self.h / 2.0

    @property
    def area(self) -> float:
        return self.w * self.h

    @property
    def right(self) -> float:
        return self.x + self.w


# ---------------------------------------------------------------------------
# YuNet face detector (module-level singleton, initialised lazily)
# ---------------------------------------------------------------------------

_yunet_detector: Optional[cv2.FaceDetectorYN] = None
_yunet_model_path: Optional[str] = None


def _get_yunet_detector(frame_w: int, frame_h: int) -> Optional[cv2.FaceDetectorYN]:
    """Return a (re-used or newly created) YuNet detector sized for this frame.

    Returns None if no model file can be found.
    """
    global _yunet_detector, _yunet_model_path

    # Find model
    if _yunet_model_path is None:
        for p in YUNET_MODEL_SEARCH_PATHS:
            if p.exists() and p.stat().st_size > 0:
                _yunet_model_path = str(p)
                break
        else:
            return None  # no model available

    # Create or resize
    if _yunet_detector is None:
        _yunet_detector = cv2.FaceDetectorYN_create(
            _yunet_model_path, "", (frame_w, frame_h),
            score_threshold=0.5, nms_threshold=0.3,
        )
    else:
        _yunet_detector.setInputSize((frame_w, frame_h))

    return _yunet_detector


def _detect_faces_in_frame(frame: np.ndarray) -> list[FaceBox]:
    """Run YuNet face detection on a single BGR frame.

    Returns a list of FaceBox (normalised coords).  Returns [] if the model
    is unavailable or no faces are found.
    """
    h, w = frame.shape[:2]
    detector = _get_yunet_detector(w, h)
    if detector is None:
        return []

    _, faces = detector.detect(frame)
    if faces is None or len(faces) == 0:
        return []

    result: list[FaceBox] = []
    for f in faces:
        fx, fy, fw, fh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
        # Clamp and normalise
        fx = max(0.0, fx / w)
        fy = max(0.0, fy / h)
        fw = min(float(f[2]) / w, 1.0 - fx)
        fh = min(float(f[3]) / h, 1.0 - fy)
        if fw > 0 and fh > 0:
            result.append(FaceBox(x=fx, y=fy, w=fw, h=fh))

    return result


# ---------------------------------------------------------------------------
# Blur detection helper
# ---------------------------------------------------------------------------

def _laplacian_face_crop(
    frame: np.ndarray,
    face: FaceBox,
    src_w: int,
    src_h: int,
) -> float:
    """Return Laplacian variance on the face crop ROI.

    Measures sharpness of the face region only — background bokeh must not
    penalise a sharp face (per spec R1.3).

    Returns -1.0 if the face region is too small to measure reliably.
    """
    x0 = max(0, int(face.x * src_w))
    y0 = max(0, int(face.y * src_h))
    x1 = min(src_w, int((face.x + face.w) * src_w))
    y1 = min(src_h, int((face.y + face.h) * src_h))
    if x1 - x0 < 10 or y1 - y0 < 10:
        return -1.0
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return -1.0
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


# ---------------------------------------------------------------------------
# Face-margin guard helpers
# ---------------------------------------------------------------------------

def _margin_valid_cx_range(
    face: FaceBox,
    crop_box_w: int,
    src_w: int,
    half_crop: int,
) -> tuple[int, int]:
    """Return the (min_cx, max_cx) pixel range that places *face* FULLY within
    the central 80% of the crop box (10% margin each side).

    Geometry (crop box spans [x_offset, x_offset + crop_box_w] in source pixels,
    where x_offset = cx_px - half_crop):

      face_left_crop  = face.x      * src_w - x_offset
      face_right_crop = face.right  * src_w - x_offset

    Margin constraints (face bbox sits fully within central 80%):
      face_left_crop  >= margin_px           → cx_px <= face.x     * src_w - margin_px + half_crop
      face_right_crop <= crop_box_w - margin → cx_px >= face.right * src_w - (crop_box_w - margin_px) + half_crop

    The result is also clamped to [half_crop, src_w - half_crop] so the crop
    box stays within the source frame.
    """
    margin_px = _FACE_MARGIN_SIDE_FRAC * crop_box_w
    # cx must be ≥ this so face.right is within the right margin
    min_cx = int(face.right * src_w - (crop_box_w - margin_px) + half_crop)
    # cx must be ≤ this so face.x is within the left margin
    max_cx = int(face.x * src_w - margin_px + half_crop)
    # Clamp to source bounds
    min_cx = max(half_crop, min_cx)
    max_cx = min(src_w - half_crop, max_cx)
    return min_cx, max_cx


def _is_face_within_margin_px(
    face: FaceBox,
    cx_px: int,
    crop_box_w: int,
    half_crop: int,
    src_w: int,
) -> bool:
    """True if *face* bbox sits fully within the central 80% of the crop box."""
    x_offset = cx_px - half_crop
    face_left_crop = face.x * src_w - x_offset
    face_right_crop = face.right * src_w - x_offset
    margin_px = _FACE_MARGIN_SIDE_FRAC * crop_box_w
    return face_left_crop >= margin_px and face_right_crop <= crop_box_w - margin_px


def _headroom_ok(face: FaceBox) -> bool:
    """True if the face top edge is at least _FACE_HEADROOM_FRAC from the top of
    the source frame (15% headroom above the face)."""
    return face.y >= _FACE_HEADROOM_FRAC


# ---------------------------------------------------------------------------
# Active speaker selection
# ---------------------------------------------------------------------------

def _select_active_speaker(
    frames: list[Optional[np.ndarray]],
    frame_faces: list[list[FaceBox]],
    timestamps: list[float],
    src_w: int,
    src_h: int,
    log: logging.Logger,
) -> Optional[float]:
    """Select the active speaker's face center_x (normalised 0..1).

    Selection hierarchy:
    1. If REFRAME_ASD=1: attempt LR-ASD from render.asd (falls back on any error).
    2. Improved heuristic: largest+most-central face (area * centrality score).
    3. Fallback: strictly largest face by area.

    Returns None if no faces are available.
    """
    all_faces_flat = [f for ff in frame_faces for f in ff]
    if not all_faces_flat:
        return None

    # Single face: no selection needed
    max_faces = max(len(ff) for ff in frame_faces)
    if max_faces == 1:
        xs = [ff[0].center_x for ff in frame_faces if ff]
        smoothed = _gaussian_smooth(xs, _SMOOTH_SIGMA_SAMPLES)
        return float(np.mean(smoothed)) if smoothed else None

    # Multi-face: try ASD first, then heuristics
    if _REFRAME_ASD_ENABLED:
        try:
            from render.asd import select_active_speaker_asd  # noqa: PLC0415
            cx = select_active_speaker_asd(frames, frame_faces, timestamps, src_w, src_h, log)
            if cx is not None:
                log.debug("reframe: LR-ASD selected speaker cx=%.3f", cx)
                return cx
        except Exception as asd_exc:
            log.debug("reframe: LR-ASD unavailable (%s); using heuristic", asd_exc)

    # Mouth-variance heuristic (original, more precise)
    cx = _mouth_movement_variance_pixel(frames, frame_faces, src_w, src_h, log)
    if cx is not None:
        return cx

    # Improved fallback: largest+most-central face (spec R1.4 fallback chain)
    return _select_largest_and_most_central(frame_faces)


def _select_largest_and_most_central(
    frame_faces: list[list[FaceBox]],
) -> Optional[float]:
    """Improved fallback: pick the face with the highest (area * centrality) score.

    centrality = 1 - 2 * |center_x - 0.5| (0 at edge, 1 at center).
    This prefers large faces close to horizontal center.
    """
    best_score = -1.0
    best_cx: Optional[float] = None
    for ff in frame_faces:
        for face in ff:
            centrality = 1.0 - 2.0 * abs(face.center_x - 0.5)
            score = face.area * centrality
            if score > best_score:
                best_score = score
                best_cx = face.center_x
    return best_cx


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def reframe_segment(
    source: Path,
    out_video: Path,
    out_audio: Path,
    start: float,
    duration: float,
    out_w: int,
    out_h: int,
    log: logging.Logger,
) -> None:
    """Cut [start, start+duration] from *source* and reframe to out_w×out_h with
    active-speaker tracking, blur rejection, and face-margin guard.

    Writes the reframed 9:16 video to *out_video* and a 16-kHz mono WAV
    extracted from it to *out_audio*.

    Falls back to center crop if face detection finds no non-blurry faces
    anywhere (e.g. animated content).  The design gate (LLM ranker) is
    responsible for excluding animation — this module does not duplicate that check.
    """
    with tempfile.TemporaryDirectory(prefix="reframe_") as _tmpdir:
        tmp = Path(_tmpdir)

        # Step 1: Extract raw segment (accurate seek via re-encode)
        raw_seg = tmp / "raw_seg.mp4"
        _extract_raw_segment(source, raw_seg, start, duration, log)

        # Probe source dimensions and fps from the raw segment
        src_w, src_h, src_fps = _probe_video(raw_seg)
        log.info(
            "reframe: src=%dx%d @%.3ffps target=%dx%d",
            src_w, src_h, src_fps, out_w, out_h,
        )

        # Compute how wide (in source pixels) the crop box must be to produce
        # an out_w:out_h output after scaling.
        crop_box_w = int(round(src_h * out_w / out_h))
        if crop_box_w >= src_w:
            log.info(
                "reframe: source aspect already ≤ target (crop_box_w=%d >= src_w=%d);"
                " using center crop",
                crop_box_w, src_w,
            )
            _center_crop_and_audio(raw_seg, out_video, out_audio, out_w, out_h, log)
            return

        # Step 2: Scene detection
        scenes = _detect_scenes(raw_seg, duration, log)
        log.info("reframe: %d scene(s) detected", len(scenes))

        # Step 3, 4, 5, 6, 7: Per-scene face analysis → virtual camera + margin guard
        # scene_crops: (start_s, end_s, keyframes) where keyframes=[(t, cx_px), ...]
        scene_crops: list[tuple[float, float, list[tuple[float, int]]]] = []
        any_face_found = False
        half_crop = crop_box_w // 2

        for scene_start, scene_end in scenes:
            face_found, keyframes = _analyze_scene(
                raw_seg, scene_start, scene_end,
                src_w, src_h, src_fps,
                crop_box_w, half_crop, log,
            )
            if face_found:
                any_face_found = True
            scene_crops.append((scene_start, scene_end, keyframes))

        if not any_face_found:
            if _yunet_model_path is None:
                log.warning(
                    "reframe: YuNet model NOT FOUND at any of %s — face detection "
                    "disabled, falling back to center crop. Ship the ONNX model "
                    "into the container or set REFRAME_YUNET_MODEL_PATH.",
                    [str(p) for p in YUNET_MODEL_SEARCH_PATHS],
                )
            else:
                log.info(
                    "reframe: model loaded (%s) but no non-blurry faces detected "
                    "anywhere; falling back to center crop", _yunet_model_path,
                )
            _center_crop_and_audio(raw_seg, out_video, out_audio, out_w, out_h, log)
            return

        # Step 8: Apply per-scene crops and concat
        log.info("reframe: applying %d per-scene crop(s)", len(scene_crops))
        _apply_per_scene_crops(
            raw_seg, scene_crops, crop_box_w, src_h, out_w, out_h, out_video, log
        )

        # Step 9: Extract 16-kHz WAV from reframed output
        _extract_wav(out_video, out_audio, duration, log)


# ---------------------------------------------------------------------------
# Segment extraction
# ---------------------------------------------------------------------------

def _extract_raw_segment(
    source: Path,
    out: Path,
    start: float,
    duration: float,
    log: logging.Logger,
) -> None:
    """Extract [start, start+duration] from *source* into *out*.

    Uses a lightweight re-encode (ultrafast/CRF-18) so downstream seeks
    via cv2.VideoCapture.set(POS_MSEC) land on accurate keyframes.
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(source),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"reframe: raw segment extraction failed "
            f"(start={start}, dur={duration}): {r.stderr[-1500:]}"
        )
    log.info("reframe: raw segment extracted → %s", out.name)


# ---------------------------------------------------------------------------
# Video probing
# ---------------------------------------------------------------------------

def _probe_video(path: Path) -> tuple[int, int, float]:
    """Return (width, height, fps).  Falls back to (1920, 1080, 30.0)."""
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    try:
        for s in json.loads(r.stdout).get("streams", []):
            if s.get("codec_type") == "video":
                w = int(s.get("width", 1920))
                h = int(s.get("height", 1080))
                fps_str = s.get("r_frame_rate", "30/1")
                try:
                    num_s, den_s = fps_str.split("/")
                    fps = float(num_s) / max(float(den_s), 1.0)
                except Exception:
                    fps = 30.0
                return w, h, fps
    except Exception:
        pass
    return 1920, 1080, 30.0


# ---------------------------------------------------------------------------
# Scene detection
# ---------------------------------------------------------------------------

def _detect_scenes(
    video_path: Path,
    duration: float,
    log: logging.Logger,
) -> list[tuple[float, float]]:
    """Detect scene cuts using PySceneDetect AdaptiveDetector.

    Returns (start_s, end_s) intervals spanning [0, duration].
    Falls back to a single scene on any error.
    """
    try:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import AdaptiveDetector

        video = open_video(str(video_path))
        sm = SceneManager()
        sm.add_detector(AdaptiveDetector())
        sm.detect_scenes(video=video, show_progress=False)
        raw = sm.get_scene_list()

        scenes: list[tuple[float, float]] = []
        for start_tc, end_tc in raw:
            s = float(start_tc.get_seconds())
            e = float(end_tc.get_seconds())
            if e > s:
                scenes.append((s, e))

        if not scenes:
            return [(0.0, duration)]

        # Clamp to [0, duration]; fill leading/trailing gaps
        scenes[0] = (0.0, scenes[0][1])
        scenes[-1] = (scenes[-1][0], duration)
        return scenes

    except Exception as exc:
        log.warning("reframe: scene detection failed (%s); using single scene", exc)
        return [(0.0, duration)]


# ---------------------------------------------------------------------------
# Per-scene face analysis (v2 — with blur, mode selection, margin guard)
# ---------------------------------------------------------------------------

def _analyze_scene(
    video_path: Path,
    scene_start: float,
    scene_end: float,
    src_w: int,
    src_h: int,
    src_fps: float,
    crop_box_w: int,
    half_crop: int,
    log: logging.Logger,
) -> tuple[bool, list[tuple[float, int]]]:
    """Analyse a scene and return per-keyframe crop positions.

    Returns:
        (face_found, keyframes) where
        face_found  — True if at least one non-blurry face was detected.
        keyframes   — list of (timestamp_in_clip_s, cx_px_in_source).
                      Single-entry for constant mode; multi-entry for tracking.

    Steps:
    1. Sample frames across the scene at _FACE_SAMPLE_FPS.
    2. Detect faces in each frame.
    3. Filter blurry frames: Laplacian variance on face crop < _FACE_BLUR_LAP_THRESHOLD.
    4. Select active speaker from non-blurry frames.
    5. Determine mode: stationary (stddev < 5% src_w) or tracking.
    6. Apply margin guard + re-anchor; switch to tracking if constant mode fails.
    7. Return keyframes clamped to source bounds.
    """
    scene_dur = max(scene_end - scene_start, 0.0)
    if scene_dur <= 0.0:
        return False, [(scene_start, src_w // 2)]

    n_samples = max(2, int(math.ceil(scene_dur * _FACE_SAMPLE_FPS)))
    if n_samples == 1:
        timestamps = [scene_start + scene_dur / 2.0]
    else:
        timestamps = [
            scene_start + scene_dur * i / (n_samples - 1)
            for i in range(n_samples)
        ]

    # --- Sample frames ---
    cap = cv2.VideoCapture(str(video_path))
    frames: list[Optional[np.ndarray]] = []
    frame_faces: list[list[FaceBox]] = []

    for ts in timestamps:
        cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000.0)
        ret, frame = cap.read()
        if not ret or frame is None:
            frames.append(None)
            frame_faces.append([])
            continue
        faces = _detect_faces_in_frame(frame)
        frames.append(frame)
        frame_faces.append(faces)

    cap.release()

    # --- Blur filtering (per-face Laplacian on face crop) ---
    n_blurry = 0
    n_total_faces = 0
    blurry_mask: list[bool] = []  # True = this sample-frame's best face is blurry
    filtered_faces: list[list[FaceBox]] = []  # faces in non-blurry frames

    for idx, (frame, ff) in enumerate(zip(frames, frame_faces)):
        if frame is None or not ff:
            blurry_mask.append(False)
            filtered_faces.append([])
            continue
        # Pick the largest face for blur measurement
        primary = max(ff, key=lambda f: f.area)
        n_total_faces += 1
        lv = _laplacian_face_crop(frame, primary, src_w, src_h)
        if 0 <= lv < _FACE_BLUR_LAP_THRESHOLD:
            blurry_mask.append(True)
            filtered_faces.append([])
            n_blurry += 1
        else:
            blurry_mask.append(False)
            filtered_faces.append(ff)

    if n_total_faces > 0:
        log.info(
            "reframe: scene [%.2f-%.2f] blur: %d/%d face-frames blurry "
            "(face LV threshold=%.0f)",
            scene_start, scene_end, n_blurry, n_total_faces, _FACE_BLUR_LAP_THRESHOLD,
        )

    # Non-blurry frames with faces
    valid_frames = [f for f, m in zip(frames, blurry_mask) if not m]
    valid_ff = [ff for ff, m in zip(frame_faces, blurry_mask) if not m]
    valid_ts = [t for t, m in zip(timestamps, blurry_mask) if not m]

    all_valid_faces_flat = [f for ff in valid_ff for f in ff]
    if not all_valid_faces_flat:
        # All faces blurry or no faces at all
        return False, [(scene_start, src_w // 2)]

    # --- Active speaker selection ---
    cx_norm = _select_active_speaker(
        valid_frames, valid_ff, valid_ts, src_w, src_h, log
    )
    if cx_norm is None:
        return False, [(scene_start, src_w // 2)]

    # Per-sample face centers for the active speaker (approximate: track the
    # face closest to cx_norm in each non-blurry frame)
    speaker_centers: list[tuple[float, float]] = []  # (timestamp, cx_norm)
    for ts_i, ff_i in zip(valid_ts, valid_ff):
        if not ff_i:
            continue
        best_face = min(ff_i, key=lambda f: abs(f.center_x - cx_norm))
        speaker_centers.append((ts_i, best_face.center_x))

    if not speaker_centers:
        return True, [(scene_start, int(round(cx_norm * src_w)))]

    sc_arr = np.array(speaker_centers)  # shape (N, 2): [[t, cx], ...]
    face_xs = sc_arr[:, 1]

    # --- Mode selection ---
    stddev = float(np.std(face_xs)) if len(face_xs) > 1 else 0.0
    stationary = stddev < _STATIONARY_STDDEV_FRAC

    log.info(
        "reframe: scene [%.2f-%.2f] speaker cx stddev=%.4f (threshold=%.4f) → %s",
        scene_start, scene_end, stddev, _STATIONARY_STDDEV_FRAC,
        "stationary" if stationary else "tracking",
    )

    if stationary:
        # Constant crop: try median cx with margin guard
        cx_median = float(np.median(face_xs))
        keyframes = _apply_constant_with_margin_guard(
            cx_median, speaker_centers, valid_ff, valid_ts,
            scene_start, scene_end, crop_box_w, half_crop, src_w, src_fps, log,
        )
        return True, keyframes
    else:
        # Tracking: polyfit → keyframes → margin guard → max-shift clamp
        keyframes = _compute_tracking_keyframes(
            speaker_centers, valid_ff, valid_ts,
            scene_start, scene_end, src_fps,
            crop_box_w, half_crop, src_w, log,
        )
        return True, keyframes


def _apply_constant_with_margin_guard(
    cx_norm: float,
    speaker_centers: list[tuple[float, float]],  # (t, cx_norm)
    valid_ff: list[list[FaceBox]],
    valid_ts: list[float],
    scene_start: float,
    scene_end: float,
    crop_box_w: int,
    half_crop: int,
    src_w: int,
    src_fps: float,
    log: logging.Logger,
) -> list[tuple[float, int]]:
    """Apply constant crop with margin guard + re-anchor.

    For each non-blurry sampled frame, compute the valid cx range that places
    the face fully within the central 80%.  Intersect all ranges to find a
    single constant cx.  If no valid constant cx exists → upgrade to tracking.

    Returns [(scene_start, cx_px)].
    """
    proposed_cx_px = int(round(cx_norm * src_w))
    proposed_cx_px = max(half_crop, min(src_w - half_crop, proposed_cx_px))

    # Collect per-frame valid cx ranges
    global_min_cx = half_crop
    global_max_cx = src_w - half_crop
    n_violations = 0
    headroom_warnings = 0

    for ts_i, ff_i in zip(valid_ts, valid_ff):
        if not ff_i:
            continue
        # The primary speaker face (closest to proposed cx)
        primary = min(ff_i, key=lambda f: abs(f.center_x - cx_norm))

        if not _headroom_ok(primary):
            headroom_warnings += 1

        min_cx_i, max_cx_i = _margin_valid_cx_range(primary, crop_box_w, src_w, half_crop)
        if not _is_face_within_margin_px(primary, proposed_cx_px, crop_box_w, half_crop, src_w):
            n_violations += 1
            log.debug(
                "reframe: margin violation at t=%.2f face=(%.3f,%.3f,%.3f,%.3f) "
                "proposed_cx=%d valid=[%d,%d]",
                ts_i, primary.x, primary.y, primary.w, primary.h,
                proposed_cx_px, min_cx_i, max_cx_i,
            )
        # Intersect with global valid range
        global_min_cx = max(global_min_cx, min_cx_i)
        global_max_cx = min(global_max_cx, max_cx_i)

    if n_violations > 0:
        log.info(
            "reframe: scene [%.2f-%.2f] %d margin violation(s), "
            "valid constant cx range=[%d,%d]",
            scene_start, scene_end, n_violations, global_min_cx, global_max_cx,
        )

    if headroom_warnings > 0:
        log.info(
            "reframe: scene [%.2f-%.2f] %d frame(s) have <%.0f%% headroom above face",
            scene_start, scene_end, headroom_warnings, _FACE_HEADROOM_FRAC * 100,
        )

    if global_min_cx <= global_max_cx:
        # A valid constant cx exists — use midpoint of valid range
        adjusted_cx = max(global_min_cx, min(global_max_cx, proposed_cx_px))
        if adjusted_cx != proposed_cx_px:
            log.info(
                "reframe: scene [%.2f-%.2f] constant cx adjusted %d → %d (margin guard)",
                scene_start, scene_end, proposed_cx_px, adjusted_cx,
            )
        return [(scene_start, adjusted_cx)]
    else:
        # No single cx satisfies all frames → upgrade to tracking
        log.info(
            "reframe: scene [%.2f-%.2f] constant crop cannot satisfy margin for "
            "all frames → upgrading to tracking mode",
            scene_start, scene_end,
        )
        return _compute_tracking_keyframes(
            speaker_centers, valid_ff, valid_ts,
            scene_start, scene_end, src_fps,
            crop_box_w, half_crop, src_w, log,
        )


def _compute_tracking_keyframes(
    speaker_centers: list[tuple[float, float]],  # (t, cx_norm)
    valid_ff: list[list[FaceBox]],
    valid_ts: list[float],
    scene_start: float,
    scene_end: float,
    src_fps: float,
    crop_box_w: int,
    half_crop: int,
    src_w: int,
    log: logging.Logger,
) -> list[tuple[float, int]]:
    """Compute tracking keyframes via 2nd-degree polyfit, then apply margin guard
    and max-shift constraint.

    Returns [(timestamp, cx_px), ...] at _TRACKING_KEYFRAME_INTERVAL_S intervals.
    """
    scene_dur = scene_end - scene_start
    sc_arr = np.array(speaker_centers)
    ts_arr = sc_arr[:, 0]
    cx_arr = sc_arr[:, 1]

    # 2nd-degree polyfit over (t, face_center_x)
    degree = min(2, len(sc_arr) - 1) if len(sc_arr) >= 2 else 1
    if len(sc_arr) < 2:
        # Not enough samples for polyfit → constant at the one sample
        cx_px = int(round(float(cx_arr[0]) * src_w))
        cx_px = max(half_crop, min(src_w - half_crop, cx_px))
        return [(scene_start, cx_px)]

    try:
        coeffs = np.polyfit(ts_arr, cx_arr, degree)
        poly = np.poly1d(coeffs)
    except Exception as exc:
        log.warning("reframe: polyfit failed (%s); using median cx", exc)
        cx_median = float(np.median(cx_arr))
        cx_px = int(round(cx_median * src_w))
        cx_px = max(half_crop, min(src_w - half_crop, cx_px))
        return [(scene_start, cx_px)]

    # Generate keyframe timestamps
    n_kf = max(1, int(math.ceil(scene_dur / _TRACKING_KEYFRAME_INTERVAL_S)))
    kf_times = [
        scene_start + scene_dur * i / n_kf
        for i in range(n_kf)
    ]

    # Evaluate polyfit at each keyframe
    raw_keyframes: list[tuple[float, int]] = []
    for kf_t in kf_times:
        cx_norm_at_t = float(poly(kf_t))
        cx_px = int(round(cx_norm_at_t * src_w))
        cx_px = max(half_crop, min(src_w - half_crop, cx_px))
        raw_keyframes.append((kf_t, cx_px))

    # Apply margin guard per keyframe: clamp to valid range for nearest samples
    guarded_keyframes: list[tuple[float, int]] = []
    n_kf_violations = 0
    for kf_t, cx_px in raw_keyframes:
        # Find nearest valid_ts sample with faces
        nearest_ff: list[FaceBox] = []
        if valid_ts:
            idx = int(np.argmin([abs(ts - kf_t) for ts in valid_ts]))
            nearest_ff = valid_ff[idx] if idx < len(valid_ff) else []

        if nearest_ff:
            # Find the speaker face (largest+most-central)
            cx_at_kf_norm = float(poly(kf_t))
            primary = min(nearest_ff, key=lambda f: abs(f.center_x - cx_at_kf_norm))
            min_cx, max_cx = _margin_valid_cx_range(primary, crop_box_w, src_w, half_crop)
            if cx_px < min_cx or cx_px > max_cx:
                n_kf_violations += 1
                cx_px = max(min_cx, min(max_cx, cx_px))

        guarded_keyframes.append((kf_t, cx_px))

    if n_kf_violations > 0:
        log.info(
            "reframe: tracking scene [%.2f-%.2f] %d keyframe margin violation(s) clamped",
            scene_start, scene_end, n_kf_violations,
        )

    # Enforce max shift ~1px/frame between consecutive keyframes
    # At 0.5s intervals and 30fps: max shift = 1 * fps * interval = 15px
    max_shift_per_kf = _MAX_SHIFT_PX_PER_FRAME * src_fps * _TRACKING_KEYFRAME_INTERVAL_S
    clamped: list[tuple[float, int]] = [guarded_keyframes[0]]
    for i in range(1, len(guarded_keyframes)):
        prev_t, prev_cx = clamped[-1]
        cur_t, cur_cx = guarded_keyframes[i]
        dt = cur_t - prev_t
        max_shift = _MAX_SHIFT_PX_PER_FRAME * src_fps * max(dt, _TRACKING_KEYFRAME_INTERVAL_S)
        if abs(cur_cx - prev_cx) > max_shift:
            direction = 1 if cur_cx > prev_cx else -1
            cur_cx = int(prev_cx + direction * max_shift)
            cur_cx = max(half_crop, min(src_w - half_crop, cur_cx))
        clamped.append((cur_t, cur_cx))

    log.info(
        "reframe: scene [%.2f-%.2f] tracking → %d keyframe(s) (polyfit degree=%d)",
        scene_start, scene_end, len(clamped), degree,
    )
    return clamped


# ---------------------------------------------------------------------------
# Mouth-movement heuristic (kept for multi-face speaker selection)
# ---------------------------------------------------------------------------

def _mouth_movement_variance_pixel(
    frames: list[Optional[np.ndarray]],
    frame_faces: list[list[FaceBox]],
    src_w: int,
    src_h: int,
    log: logging.Logger,
) -> Optional[float]:
    """Select the active speaker using pixel-difference variance in the lower
    face region (mouth area) across consecutive sample pairs.

    For each face detected (slotted by descending area across frames), we
    accumulate the mean-absolute-difference between consecutive frame pairs
    in the lower _MOUTH_ROI_FRAC of the face bounding box.  The face slot
    with the highest mean MAD is the most likely speaker.

    Returns the smoothed center_x of the most-active face, or None on failure.
    """
    try:
        n_slots = max(len(ff) for ff in frame_faces)
        valid_pairs: list[tuple[int, int]] = []
        for i in range(len(frames) - 1):
            if frames[i] is not None and frames[i + 1] is not None:
                valid_pairs.append((i, i + 1))

        if not valid_pairs:
            return None

        slot_mad: list[list[float]] = [[] for _ in range(n_slots)]
        slot_xs: list[list[float]] = [[] for _ in range(n_slots)]

        for idx_a, idx_b in valid_pairs:
            fa = frame_faces[idx_a]
            fb = frame_faces[idx_b]
            if not fa or not fb:
                continue

            sorted_a = sorted(fa, key=lambda f: f.area, reverse=True)
            sorted_b = sorted(fb, key=lambda f: f.area, reverse=True)

            for slot_idx in range(min(n_slots, len(sorted_a), len(sorted_b))):
                face_a = sorted_a[slot_idx]
                face_b = sorted_b[slot_idx]

                def _mouth_roi(face: FaceBox, fr: np.ndarray) -> Optional[np.ndarray]:
                    fh_px = int(face.h * src_h)
                    fw_px = int(face.w * src_w)
                    if fh_px < 10 or fw_px < 10:
                        return None
                    y0 = int((face.y + face.h * (1.0 - _MOUTH_ROI_FRAC)) * src_h)
                    y1 = int((face.y + face.h) * src_h)
                    x0 = int(face.x * src_w)
                    x1 = int((face.x + face.w) * src_w)
                    y0, y1 = max(0, y0), min(fr.shape[0], y1)
                    x0, x1 = max(0, x0), min(fr.shape[1], x1)
                    if y1 <= y0 or x1 <= x0:
                        return None
                    roi = fr[y0:y1, x0:x1]
                    return cv2.resize(roi, (16, 8), interpolation=cv2.INTER_AREA) if roi.size > 0 else None

                frame_a = frames[idx_a]
                frame_b = frames[idx_b]
                roi_a = _mouth_roi(face_a, frame_a)  # type: ignore[arg-type]
                roi_b = _mouth_roi(face_b, frame_b)  # type: ignore[arg-type]

                if roi_a is None or roi_b is None:
                    continue

                mad = float(np.mean(np.abs(roi_a.astype(np.float32) - roi_b.astype(np.float32))))
                slot_mad[slot_idx].append(mad)
                slot_xs[slot_idx].append(face_a.center_x)

        best_mad = -1.0
        best_cx: Optional[float] = None
        for slot_idx in range(n_slots):
            mads = slot_mad[slot_idx]
            if not mads:
                continue
            mean_mad = float(np.mean(mads))
            if mean_mad > best_mad and slot_xs[slot_idx]:
                best_mad = mean_mad
                xs = slot_xs[slot_idx]
                smoothed = _gaussian_smooth(xs, _SMOOTH_SIGMA_SAMPLES)
                best_cx = float(np.mean(smoothed))

        return best_cx

    except Exception as exc:
        log.warning("reframe: mouth-variance heuristic failed (%s); using improved fallback", exc)
        return None


# ---------------------------------------------------------------------------
# Gaussian smoothing (no scipy dependency)
# ---------------------------------------------------------------------------

def _gaussian_smooth(values: list[float], sigma: float) -> list[float]:
    """Apply 1-D Gaussian smoothing (truncated kernel, clamp boundary)."""
    n = len(values)
    if n <= 1 or sigma <= 0:
        return list(values)

    radius = max(1, int(math.ceil(sigma * 3.0)))
    kernel_raw = [math.exp(-0.5 * (i / sigma) ** 2) for i in range(-radius, radius + 1)]
    k_sum = sum(kernel_raw)
    kernel = [k / k_sum for k in kernel_raw]

    result: list[float] = []
    for i in range(n):
        acc = 0.0
        for ki, k in enumerate(kernel):
            j = max(0, min(n - 1, i + ki - radius))
            acc += values[j] * k
        result.append(acc)
    return result


# ---------------------------------------------------------------------------
# Per-scene crop + concat (supports constant and tracking keyframes)
# ---------------------------------------------------------------------------

def _apply_per_scene_crops(
    video: Path,
    scenes: list[tuple[float, float, list[tuple[float, int]]]],
    crop_box_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    out_video: Path,
    log: logging.Logger,
) -> None:
    """Encode one (sub-)segment per keyframe interval, then concat.

    For constant-crop scenes (single keyframe), generates one segment per scene.
    For tracking scenes (multiple keyframes), generates one sub-segment per
    keyframe interval.  All segments use the same codec/resolution so the
    concat demuxer can join them with ``-c copy``.
    """
    with tempfile.TemporaryDirectory(prefix="scene_segs_") as _tmpdir:
        tmp = Path(_tmpdir)
        seg_files: list[Path] = []
        global_idx = 0

        for scene_idx, (scene_start, scene_end, keyframes) in enumerate(scenes):
            if scene_end <= scene_start:
                continue

            if len(keyframes) == 1:
                # Constant crop: encode the whole scene with the single cx_px
                _, cx_px = keyframes[0]
                seg_out = tmp / f"seg_{global_idx:05d}.mp4"
                seg_dur = scene_end - scene_start
                ok = _encode_crop_segment(
                    video, seg_out,
                    scene_start, seg_dur,
                    cx_px, crop_box_w, src_h, out_w, out_h,
                    log, label=f"scene{scene_idx}",
                )
                if ok:
                    seg_files.append(seg_out)
                global_idx += 1
            else:
                # Tracking: one sub-segment per keyframe interval
                for kf_idx, (kf_t, cx_px) in enumerate(keyframes):
                    # Determine sub-segment end
                    if kf_idx + 1 < len(keyframes):
                        kf_end = keyframes[kf_idx + 1][0]
                    else:
                        kf_end = scene_end
                    seg_dur = kf_end - kf_t
                    if seg_dur <= 0.01:
                        continue
                    seg_out = tmp / f"seg_{global_idx:05d}.mp4"
                    ok = _encode_crop_segment(
                        video, seg_out,
                        kf_t, seg_dur,
                        cx_px, crop_box_w, src_h, out_w, out_h,
                        log, label=f"scene{scene_idx}_kf{kf_idx}",
                    )
                    if ok:
                        seg_files.append(seg_out)
                    global_idx += 1

        if not seg_files:
            raise RuntimeError("reframe: all scene segments failed to encode")

        if len(seg_files) == 1:
            shutil.copy2(str(seg_files[0]), str(out_video))
            log.info("reframe: single segment → %s", out_video.name)
            return

        # Concat with demuxer + stream copy (all same codec, resolution, SAR)
        concat_list = tmp / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{f}'" for f in seg_files),
            encoding="utf-8",
        )
        cmd_cat = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            "-movflags", "+faststart",
            str(out_video),
        ]
        r = subprocess.run(cmd_cat, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"reframe: concat failed: {r.stderr[-1500:]}")
        log.info("reframe: concatenated %d segment(s) → %s", len(seg_files), out_video.name)


def _encode_crop_segment(
    video: Path,
    seg_out: Path,
    seg_start: float,
    seg_dur: float,
    cx_px: int,
    crop_box_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    log: logging.Logger,
    label: str = "seg",
) -> bool:
    """Encode one crop segment.  Returns True on success, False on failure."""
    x_offset = max(0, cx_px - crop_box_w // 2)
    vf = (
        f"crop={crop_box_w}:{src_h}:{x_offset}:0,"
        f"scale={out_w}:{out_h}:flags=lanczos,"
        f"setsar=1"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seg_start),
        "-i", str(video),
        "-t", str(seg_dur),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(seg_out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("reframe: %s encode failed: %s", label, r.stderr[-400:])
        return False
    return True


# ---------------------------------------------------------------------------
# Center-crop fallback
# ---------------------------------------------------------------------------

def _center_crop_and_audio(
    video: Path,
    out_video: Path,
    out_audio: Path,
    out_w: int,
    out_h: int,
    log: logging.Logger,
) -> None:
    """Standard center-crop reframe used when no non-blurry faces are found."""
    scale_filter = (
        f"scale=-2:{out_h}:flags=lanczos,"
        f"crop={out_w}:{out_h},"
        f"setsar=1"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        "-vf", scale_filter,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(out_video),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"reframe: center-crop fallback failed: {r.stderr[-1500:]}")
    log.info("reframe: center crop complete → %s", out_video.name)
    _extract_wav(out_video, out_audio, None, log)


# ---------------------------------------------------------------------------
# WAV extraction
# ---------------------------------------------------------------------------

def _extract_wav(
    video: Path,
    out_audio: Path,
    duration: Optional[float],
    log: logging.Logger,
) -> None:
    """Extract 16-kHz mono WAV from *video* to *out_audio*."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        str(out_audio),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("reframe: WAV extraction failed; writing silent fallback")
        _write_silent_wav(out_audio, duration or 30.0)
    else:
        log.info("reframe: WAV extracted → %s", out_audio.name)


def _write_silent_wav(path: Path, duration: float) -> None:
    """Write a silent 16-kHz mono WAV of the given duration."""
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-t", str(duration),
        "-acodec", "pcm_s16le",
        str(path),
    ], capture_output=True)
