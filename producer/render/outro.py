"""
outro.py — Normalize and concatenate an outro clip onto the main rendered clip.

The outro (typically a short branded end card) must be normalized to exactly
match the main clip's parameters before concatenation:
  * resolution: same as cfg.template.resolution
  * frame rate: 30 fps
  * audio: AAC 44.1 kHz stereo; OR silent if audio="mute"

Concatenation uses the concat demuxer (`-f concat -safe 0`) which requires
both input files to share codec and stream parameters — hence the normalization
step.  After concat the output stream is re-packaged to MP4 with stream copy.

If outro is disabled (cfg.template.outro.enabled is False) or the outro file
does not exist, the main clip is returned as-is.
"""

import logging
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CONCAT_FPS = 30
_CONCAT_AUDIO_RATE = 44100
_CONCAT_AUDIO_CHANNELS = 2


def _has_audio_stream(src: Path) -> bool:
    """True if *src* contains at least one audio stream (via ffprobe)."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return bool(result.stdout.strip())
    except Exception as exc:
        log.warning("ffprobe audio check failed for %s: %s", src, exc)
        return True  # assume audio present; ffmpeg's 0:a map will surface errors


def append_outro(
    main_video: Path,
    cfg_template: Any,
    workdir: Path,
    *,
    stem: str = "with_outro",
) -> Path:
    """Append the configured outro to *main_video* if enabled and available.

    Parameters
    ----------
    main_video   : Path – composited clip from overlay.py.
    cfg_template : CampaignConfig template namespace.
    workdir      : Path – temp dir for intermediate files.

    Returns
    -------
    Path to the final MP4 (may be *main_video* unchanged if no outro).
    """
    outro_cfg = getattr(cfg_template, "outro", None)
    if not outro_cfg or not getattr(outro_cfg, "enabled", False):
        log.info("append_outro: outro disabled; returning main clip.")
        return main_video

    outro_src = Path(getattr(outro_cfg, "clip", ""))
    if not outro_src.exists():
        log.warning(
            "append_outro: outro file '%s' not found; skipping concat.",
            outro_src,
        )
        return main_video

    out_w, out_h = cfg_template.resolution
    mute_outro = str(getattr(outro_cfg, "audio", "keep")).lower() == "mute"

    output_path = workdir / f"{stem}.mp4"
    norm_main = workdir / "norm_main.mp4"
    norm_outro = workdir / "norm_outro.mp4"
    concat_list = workdir / "concat_list.txt"

    # ------------------------------------------------------------------
    # Normalize main clip to standard params
    # ------------------------------------------------------------------
    _normalize_clip(main_video, norm_main, out_w, out_h, mute=False)

    # ------------------------------------------------------------------
    # Normalize outro clip
    # ------------------------------------------------------------------
    _normalize_clip(outro_src, norm_outro, out_w, out_h, mute=mute_outro)

    # ------------------------------------------------------------------
    # Write concat list
    # ------------------------------------------------------------------
    concat_list.write_text(
        f"file '{norm_main.resolve()}'\n"
        f"file '{norm_outro.resolve()}'\n",
        encoding="utf-8",
    )

    # ------------------------------------------------------------------
    # Concat (stream copy — both clips already share codec/params)
    # ------------------------------------------------------------------
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"append_outro: concat failed (exit {result.returncode}):\n"
            f"STDERR: {result.stderr[-3000:]}"
        )

    log.info(
        "append_outro: concatenated %s + outro → %s",
        main_video.name, output_path.name,
    )
    return output_path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_clip(
    src: Path,
    dst: Path,
    out_w: int,
    out_h: int,
    *,
    mute: bool,
) -> None:
    """Re-encode *src* to standard resolution/fps/audio, write to *dst*.

    Standard parameters:
      video: libx264 veryfast crf 20, {out_w}x{out_h}, 30 fps
      audio: AAC 192 k, 44100 Hz, stereo (or silent if mute=True)
    """
    vf = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={_CONCAT_FPS}"
    )

    # A silent audio track is injected when muting OR when the source has no
    # audio stream — the concat demuxer requires both clips to carry matching
    # streams, so every normalized clip must have exactly one audio track.
    if mute or not _has_audio_stream(src):
        audio_args = [
            "-f", "lavfi", "-i", f"anullsrc=r={_CONCAT_AUDIO_RATE}:cl=stereo",
            "-map", "0:v",
            "-map", "1:a",
            "-shortest",
        ]
    else:
        audio_args = [
            "-map", "0:v",
            "-map", "0:a?",
        ]

    cmd = (
        ["ffmpeg", "-y", "-i", str(src)]
        + audio_args
        + [
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", str(_CONCAT_AUDIO_RATE),
            "-ac", str(_CONCAT_AUDIO_CHANNELS),
            "-movflags", "+faststart",
            str(dst),
        ]
    )

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"_normalize_clip: ffmpeg failed on '{src.name}' "
            f"(exit {result.returncode}):\n"
            f"STDERR: {result.stderr[-3000:]}"
        )
