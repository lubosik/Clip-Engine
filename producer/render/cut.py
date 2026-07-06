"""
cut.py — Precise time-range extraction from a source video.

Re-encodes with -preset veryfast -crf 20 to ensure frame accuracy.
Stream copy (-c copy) is intentionally NOT used: seeking to a non-keyframe
with stream copy would shift the start point to the nearest preceding keyframe,
producing clips that begin earlier than requested.

Also extracts a 16-kHz mono WAV for faster-whisper word-timing fallback.
"""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def cut_clip(
    source_video: Path,
    start: float,
    end: float,
    workdir: Path,
    *,
    stem: str = "cut",
) -> dict:
    """Cut the [start, end] window from *source_video* and write to *workdir*.

    Parameters
    ----------
    source_video : Path
        Full path to the downloaded source file (any format ffmpeg can read).
    start : float
        Clip start in seconds, relative to the source video.
    end : float
        Clip end in seconds, relative to the source video.
    workdir : Path
        Temporary working directory for this clip render job.
    stem : str
        Filename stem for output files (no extension).

    Returns
    -------
    dict
        ``video_path``  – re-encoded MP4 clip, time-zero = clip start.
        ``audio_path``  – WAV file (mono, 16 kHz) for Whisper; may be a silent
                          placeholder if the source has no audio track.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    video_path = workdir / f"{stem}.mp4"
    audio_path = workdir / f"{stem}.wav"
    duration = end - start

    if duration <= 0:
        raise ValueError(
            f"Invalid clip range: start={start:.3f} end={end:.3f} "
            f"(duration={duration:.3f}s <= 0)"
        )

    # ------------------------------------------------------------------
    # 1.  Video cut — re-encode for frame accuracy
    # ------------------------------------------------------------------
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(source_video),
        "-t", str(duration),
        # Video: H.264 veryfast for speed; crf 20 for good quality
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        # Audio: AAC 192 k (kept for playback in review PWA)
        "-c:a", "aac",
        "-b:a", "192k",
        # Reset PTS so the clip starts at t=0
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(video_path),
    ]
    _run(cmd, desc="cut video")

    # ------------------------------------------------------------------
    # 2.  Extract WAV for faster-whisper (mono, 16 kHz)
    # ------------------------------------------------------------------
    cmd_audio = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-acodec", "pcm_s16le",
        str(audio_path),
    ]
    try:
        _run(cmd_audio, desc="extract audio")
    except RuntimeError as exc:
        log.warning(
            "cut_clip: audio extraction failed (%s); writing silent WAV fallback.",
            exc,
        )
        _write_silent_wav(audio_path, duration)

    log.info(
        "cut_clip: wrote %s (%.2fs) + %s",
        video_path.name,
        duration,
        audio_path.name,
    )
    return {"video_path": video_path, "audio_path": audio_path}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], desc: str) -> None:
    """Run an ffmpeg command, raising RuntimeError on non-zero exit."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed during '{desc}' (exit {result.returncode}):\n"
            f"CMD: {' '.join(cmd[:6])} ...\n"
            f"STDERR (tail): {result.stderr[-3000:]}"
        )


def _write_silent_wav(path: Path, duration: float) -> None:
    """Write a silent mono 16-kHz WAV of the requested duration."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r=16000:cl=mono",
        "-t", str(duration),
        "-acodec", "pcm_s16le",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log.error("_write_silent_wav: ffmpeg failed; %s may be missing.", path)
