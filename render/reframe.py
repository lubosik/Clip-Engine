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
4. Active-speaker selection (v1 heuristic):
   - 0 faces: center crop for this scene.
   - 1 face: track it.
   - N>1 faces: for each face, compute the pixel-difference variance in the
     lower 40% of the face bounding box across consecutive sample pairs.
     This region captures mouth movement.  The face with the highest variance
     is the likely active speaker.  Fallback: largest bounding-box area.
5. Per-scene crop center x: Gaussian-smooth the sample-frame centers WITHIN
   each scene (sigma=2 sample frames); SNAP (no smoothing) across scene cuts;
   clamp so crop box stays within frame.
6. Apply via ffmpeg: per-scene constant crop + scale → temp segment files,
   then concat with the concat demuxer (-c copy; all segs same codec/size).
7. Extract 16-kHz mono WAV from the reframed output to *out_audio*.
8. Global fallback: if no faces are detected in any scene (e.g. animated
   content), fall back to standard center crop for the entire clip.

Face detection
--------------
Primary: OpenCV FaceDetectorYN (YuNet DNN, ~228 KB ONNX model).
Model is loaded from the first path that exists in YUNET_MODEL_SEARCH_PATHS.
Falls back to Haar cascade if no model path resolves (no detection).

MediaPipe / TalkNet-ASD upgrade path
-------------------------------------
This v1 uses a pixel-variance heuristic for mouth-movement detection.
Two structured upgrade points are documented below:

  A) MediaPipe FaceLandmarker (face mesh, lip landmark 13↔14 distance variance):
     Replace `_mouth_movement_variance_pixel` with a call to
     `mediapipe.tasks.python.vision.FaceLandmarker`.  Requires bundling
     the `face_landmarker.task` model (3.6 MB from storage.googleapis.com).
     MediaPipe 0.10.x needs `libGLESv2` in the runtime environment.

  B) TalkNet-ASD (TaoRuijie/TalkNet-ASD — audio-visual active speaker):
     Slot in as `ActiveSpeakerDetector.select()` called from `_analyze_scene`.
     The rest of the pipeline (scene detection, virtual camera, ffmpeg concat)
     is unchanged.  Interface:

         class ActiveSpeakerDetector(Protocol):
             def select(
                 self,
                 frame_samples: list[np.ndarray],    # BGR frames
                 scene_face_boxes: list[list[FaceBox]],
                 audio_wav: Path,
                 scene_start: float,
                 scene_end: float,
             ) -> float | None:
                 \"\"\"Return active speaker center_x in [0,1], or None.\"\"\"
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

_FACE_SAMPLE_FPS: float = 3.0      # face-detection samples per second
_SMOOTH_SIGMA_SAMPLES: float = 2.0  # Gaussian σ in sample-frame units
_MOUTH_ROI_FRAC: float = 0.4        # lower fraction of face bbox used for mouth-variance

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
    active-speaker tracking.

    Writes the reframed 9:16 video to *out_video* and a 16-kHz mono WAV
    extracted from it to *out_audio*.

    Falls back to center crop if face detection finds no faces anywhere
    (e.g. animated content).  The design gate (LLM ranker) is responsible for
    excluding animation — this module does not duplicate that check.
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
        # an out_w:out_h output after scaling.  We crop a
        # (src_h * out_w/out_h) × src_h box from the source, then scale.
        # If the source is narrower than the required box, fall back to
        # center-crop (the full-width scale path).
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

        # Step 3 & 4: Per-scene face analysis → per-scene crop-center x (pixels)
        scene_crops: list[tuple[float, float, int]] = []  # (start_s, end_s, cx_px)
        any_face_found = False
        half_crop = crop_box_w // 2

        for scene_start, scene_end in scenes:
            cx_norm = _analyze_scene(
                raw_seg, scene_start, scene_end, src_w, src_h, src_fps, log
            )
            if cx_norm is not None:
                any_face_found = True
                cx_px = int(round(cx_norm * src_w))
            else:
                cx_px = src_w // 2  # center fallback for faceless scene

            # Step 5: clamp so crop box stays within frame
            cx_px = max(half_crop, min(src_w - half_crop, cx_px))
            scene_crops.append((scene_start, scene_end, cx_px))

        if not any_face_found:
            # Distinguish "the detector never loaded" (a deploy/path bug) from
            # "the model ran but genuinely found no faces" so this failure mode
            # is never silent again.
            if _yunet_model_path is None:
                log.warning(
                    "reframe: YuNet model NOT FOUND at any of %s — face detection "
                    "disabled, falling back to center crop. Ship the ONNX model "
                    "into the container or set REFRAME_YUNET_MODEL_PATH.",
                    [str(p) for p in YUNET_MODEL_SEARCH_PATHS],
                )
            else:
                log.info(
                    "reframe: model loaded (%s) but no faces detected anywhere; "
                    "falling back to center crop", _yunet_model_path,
                )
            _center_crop_and_audio(raw_seg, out_video, out_audio, out_w, out_h, log)
            return

        # Step 6: Apply per-scene crops and concat
        log.info("reframe: applying %d per-scene crop(s)", len(scene_crops))
        _apply_per_scene_crops(
            raw_seg, scene_crops, crop_box_w, src_h, out_w, out_h, out_video, log
        )

        # Step 7: Extract 16-kHz WAV from reframed output
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
# Per-scene face analysis
# ---------------------------------------------------------------------------

def _analyze_scene(
    video_path: Path,
    scene_start: float,
    scene_end: float,
    src_w: int,
    src_h: int,
    src_fps: float,
    log: logging.Logger,
) -> Optional[float]:
    """Return the active speaker's face center_x (normalised 0..1), or None.

    Detection uses OpenCV YuNet DNN (no display/OpenGL deps).
    Active-speaker heuristic: pixel-difference variance in the lower face
    region (mouth area) across consecutive sample pairs.
    Falls back to largest bounding-box area when the heuristic fails.

    Upgrade path to MediaPipe FaceLandmarker or TalkNet-ASD:
      see module docstring — swap in as a replacement for
      `_mouth_movement_variance_pixel`.
    """
    scene_dur = max(scene_end - scene_start, 0.0)
    if scene_dur <= 0.0:
        return None

    n_samples = max(2, int(math.ceil(scene_dur * _FACE_SAMPLE_FPS)))
    if n_samples == 1:
        timestamps = [scene_start + scene_dur / 2.0]
    else:
        timestamps = [
            scene_start + scene_dur * i / (n_samples - 1)
            for i in range(n_samples)
        ]

    # Collect frames and face boxes
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

    all_faces_flat = [f for ff in frame_faces for f in ff]
    if not all_faces_flat:
        return None

    max_faces_per_frame = max(len(ff) for ff in frame_faces)

    if max_faces_per_frame == 1:
        # Single face: smooth the center_x path
        xs = [ff[0].center_x for ff in frame_faces if ff]
        smoothed = _gaussian_smooth(xs, _SMOOTH_SIGMA_SAMPLES)
        return float(np.mean(smoothed)) if smoothed else None

    # Multiple faces: mouth-movement heuristic
    cx = _mouth_movement_variance_pixel(frames, frame_faces, src_w, src_h, log)
    if cx is not None:
        return cx

    # Fallback: largest face by area
    best_area = -1.0
    best_cx: Optional[float] = None
    for ff in frame_faces:
        for face in ff:
            if face.area > best_area:
                best_area = face.area
                best_cx = face.center_x
    return best_cx


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

    MediaPipe FaceLandmarker upgrade note
    --------------------------------------
    Replace this function with FaceLandmarker inference to get precise
    inner-lip-distance variance (landmark 13 ↔ 14 in the 468-point mesh).
    That provides audio-visual sync quality; this pixel heuristic is a
    pragmatic v1 that works without any additional model downloads.
    """
    try:
        n_slots = max(len(ff) for ff in frame_faces)
        # Pair up consecutive valid frames for difference computation
        valid_pairs: list[tuple[int, int]] = []  # (idx_a, idx_b)
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

            # Sort faces in each frame by area desc → slot alignment
            sorted_a = sorted(fa, key=lambda f: f.area, reverse=True)
            sorted_b = sorted(fb, key=lambda f: f.area, reverse=True)

            for slot_idx in range(min(n_slots, len(sorted_a), len(sorted_b))):
                face_a = sorted_a[slot_idx]
                face_b = sorted_b[slot_idx]

                # Crop lower _MOUTH_ROI_FRAC of each face in pixel space
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

        # Pick slot with highest mean MAD
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
        log.warning("reframe: mouth-variance heuristic failed (%s); using largest face", exc)
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
# Per-scene crop + concat
# ---------------------------------------------------------------------------

def _apply_per_scene_crops(
    video: Path,
    scenes: list[tuple[float, float, int]],   # (start_s, end_s, cx_px)
    crop_box_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    out_video: Path,
    log: logging.Logger,
) -> None:
    """Encode one segment per scene with its computed crop, then concat.

    All segments use the same codec/resolution so the concat demuxer can
    join them with ``-c copy``.
    """
    with tempfile.TemporaryDirectory(prefix="scene_segs_") as _tmpdir:
        tmp = Path(_tmpdir)
        seg_files: list[Path] = []

        for idx, (seg_start, seg_end, cx_px) in enumerate(scenes):
            seg_dur = seg_end - seg_start
            if seg_dur <= 0.0:
                continue

            # Left edge of crop box, clamped to [0, src_w - crop_box_w].
            # src_w can be approximated as crop_box_w * (src_h/out_h * out_w/out_h)...
            # but we don't have it here.  The caller already clamped cx_px to
            # [half_crop, src_w-half_crop], so x_offset is always ≥ 0.
            x_offset = max(0, cx_px - crop_box_w // 2)

            seg_out = tmp / f"seg_{idx:04d}.mp4"
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
                log.warning("reframe: segment %d encode failed: %s", idx, r.stderr[-400:])
                continue
            seg_files.append(seg_out)

        if not seg_files:
            raise RuntimeError("reframe: all scene segments failed to encode")

        if len(seg_files) == 1:
            shutil.copy2(str(seg_files[0]), str(out_video))
            log.info("reframe: single scene → %s", out_video.name)
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
        log.info("reframe: concatenated %d scenes → %s", len(seg_files), out_video.name)


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
    """Standard center-crop reframe used when no faces are found."""
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
