"""
producer/render/__init__.py — Clip render orchestrator.

Entry point
-----------
    render_clip(cfg, source_meta, clip, source_video, words, workdir) -> RenderResult

Pipeline
--------
    1.  cut.cut_clip       — frame-accurate [start, end] extraction → cut.mp4 + cut.wav
    2.  reframe.reframe_clip — face-aware 16:9→9:16 → reframed.mp4
    3.  captions.get_word_timings (if words is None) — faster-whisper on cut.wav
    4.  captions.build_ass — word-level ASS karaoke → captions.ass
    5.  overlay.apply_overlays — captions burn + hook + watermark + badge + credit → overlaid.mp4
    6.  outro.append_outro  — optional outro concat → with_outro.mp4 (or overlaid.mp4)
    7.  Thumbnail extraction — ffmpeg frame at 1 s, scaled to 480 w → thumb.jpg
    8.  Move final mp4 + thumbnail to STORAGE_DIR (via core.storage if available)
    9.  Clean up intermediate files in workdir
   10.  Return RenderResult(final_path, thumb_path)

Word timing contract
--------------------
``words`` received by render_clip is SOURCE-RELATIVE (t=0 = start of the source
video).  render_clip converts to CUT-RELATIVE (t=0 = start of the clip) by
subtracting clip["start"] before passing to build_ass.  Words that fall
entirely outside [clip_start, clip_end] are discarded.
"""

import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cut import cut_clip
from .reframe import reframe_clip
from .captions import build_ass, get_word_timings
from .overlay import apply_overlays
from .outro import append_outro

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class RenderResult:
    """Paths produced by a successful render_clip call."""
    final_path: Path
    thumb_path: Path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_clip(
    cfg: Any,
    source_meta: dict,
    clip: dict,
    source_video: Path,
    words: list[dict] | None,
    workdir: Path,
) -> RenderResult:
    """Render one clip end-to-end, returning paths to the mp4 and thumbnail.

    Parameters
    ----------
    cfg : CampaignConfig
        Full campaign config object (core.config.CampaignConfig).
    source_meta : dict
        Metadata for the source video.  Required key: ``"source_handle"``.
        Optional keys: ``"channelName"``, ``"title"``, ``"platform"``.
    clip : dict
        Clip descriptor from the ranker.  Required keys: ``"start"``,
        ``"end"``, ``"hook"``.  Optional: ``"score"``, ``"reason"``, ``"id"``.
    source_video : Path
        Full path to the downloaded source video file.
    words : list[dict] | None
        Source-relative word timings ``[{"word": str, "start": float, "end": float}]``.
        Pass ``None`` to have render_clip run faster-whisper on the cut clip.
    workdir : Path
        Temporary working directory.  Must be writable.  Intermediate files
        are written here and cleaned up after a successful render.

    Returns
    -------
    RenderResult
        ``final_path`` — final MP4 in STORAGE_DIR (or workdir on fallback).
        ``thumb_path`` — JPEG thumbnail, same directory as final_path.

    Raises
    ------
    RuntimeError
        If any stage fails.  Intermediate files are NOT cleaned up on failure
        to aid debugging.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    clip_start = float(clip["start"])
    clip_end = float(clip["end"])
    clip_id = str(clip.get("id", uuid.uuid4().hex[:12]))
    tmpl = cfg.template

    log.info(
        "render_clip: campaign=%s clip=%s [%.1fs–%.1fs]",
        getattr(cfg, "name", "?"),
        clip_id,
        clip_start,
        clip_end,
    )

    # ------------------------------------------------------------------
    # Stage 1: Cut
    # ------------------------------------------------------------------
    cut_result = cut_clip(source_video, clip_start, clip_end, workdir)
    cut_video: Path = cut_result["video_path"]
    cut_audio: Path = cut_result["audio_path"]

    # ------------------------------------------------------------------
    # Stage 2: Reframe 16:9 → 9:16
    # ------------------------------------------------------------------
    reframed_video = reframe_clip(cut_video, list(tmpl.resolution), workdir)

    # ------------------------------------------------------------------
    # Stage 3: Word timings (fallback to faster-whisper if not supplied)
    # ------------------------------------------------------------------
    if words is not None:
        cut_words = _to_cut_relative(words, clip_start, clip_end)
    else:
        log.info("render_clip: words=None; running faster-whisper on %s", cut_audio.name)
        try:
            cut_words = get_word_timings(cut_audio)
        except Exception as exc:
            log.warning(
                "render_clip: faster-whisper failed (%s); captions will be empty.", exc
            )
            cut_words = []

    # ------------------------------------------------------------------
    # Stage 4: Build ASS captions
    # ------------------------------------------------------------------
    ass_path = workdir / "captions.ass"
    if cut_words:
        build_ass(cut_words, tmpl, ass_path)
    else:
        # Write empty ASS so overlay step doesn't error
        ass_path.write_text(_empty_ass(tmpl.resolution), encoding="utf-8-sig")
        log.warning("render_clip: no word timings; captions will be blank.")

    # ------------------------------------------------------------------
    # Stage 5: Composite overlays
    # ------------------------------------------------------------------
    overlaid_video = apply_overlays(
        reframed_video, ass_path, tmpl, source_meta, clip, workdir
    )

    # ------------------------------------------------------------------
    # Stage 6: Outro
    # ------------------------------------------------------------------
    final_video = append_outro(overlaid_video, tmpl, workdir)

    # ------------------------------------------------------------------
    # Stage 7: Thumbnail (frame at 1 s, max width 480 px)
    # ------------------------------------------------------------------
    thumb_path_tmp = workdir / "thumb.jpg"
    _extract_thumbnail(final_video, thumb_path_tmp)

    # ------------------------------------------------------------------
    # Stage 8: Move to STORAGE_DIR
    # ------------------------------------------------------------------
    out_clips_dir, out_thumbs_dir = _resolve_output_dirs(workdir)
    out_clips_dir.mkdir(parents=True, exist_ok=True)
    out_thumbs_dir.mkdir(parents=True, exist_ok=True)

    final_mp4 = out_clips_dir / f"{clip_id}.mp4"
    final_thumb = out_thumbs_dir / f"{clip_id}.jpg"

    shutil.move(str(final_video), str(final_mp4))
    if thumb_path_tmp.exists():
        shutil.move(str(thumb_path_tmp), str(final_thumb))
    else:
        final_thumb = workdir / f"{clip_id}_thumb.jpg"  # placeholder path

    # ------------------------------------------------------------------
    # Stage 9: Cleanup intermediates
    # ------------------------------------------------------------------
    _cleanup_intermediates(workdir, keep={final_mp4, final_thumb})

    log.info(
        "render_clip: complete  clip=%s  mp4=%s  thumb=%s",
        clip_id, final_mp4.name, final_thumb.name,
    )
    return RenderResult(final_path=final_mp4, thumb_path=final_thumb)


# ---------------------------------------------------------------------------
# Word timing conversion
# ---------------------------------------------------------------------------

def _to_cut_relative(
    words: list[dict],
    clip_start: float,
    clip_end: float,
) -> list[dict]:
    """Convert source-relative words to cut-relative, discarding out-of-range entries.

    Words that start before clip_start or end after clip_end are discarded.
    Words that partially overlap the clip boundary are clamped.
    """
    result: list[dict] = []
    for w in words:
        w_start = float(w["start"]) - clip_start
        w_end = float(w["end"]) - clip_start
        # Discard if entirely before or after the clip
        if w_end <= 0 or w_start >= (clip_end - clip_start):
            continue
        w_start = max(0.0, w_start)
        w_end = min(clip_end - clip_start, w_end)
        if w_end > w_start:
            result.append({"word": w["word"], "start": w_start, "end": w_end})
    return result


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------

def _extract_thumbnail(video: Path, out_path: Path) -> None:
    """Extract the frame at 1 s (or mid-point for short clips), scaled to 480 px wide."""
    # Probe duration to avoid seeking past the end
    import json
    probe_cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        str(video),
    ]
    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
    try:
        duration = float(
            json.loads(probe_result.stdout).get("format", {}).get("duration", 2.0)
        )
    except Exception:
        duration = 2.0

    seek_t = min(1.0, duration * 0.1)

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek_t),
        "-i", str(video),
        "-vframes", "1",
        "-vf", "scale=480:-2",
        "-q:v", "3",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.warning(
            "_extract_thumbnail: ffmpeg failed (exit %d); no thumbnail written.",
            result.returncode,
        )


# ---------------------------------------------------------------------------
# Output directory resolution
# ---------------------------------------------------------------------------

def _resolve_output_dirs(workdir: Path) -> tuple[Path, Path]:
    """Return (clips_dir, thumbs_dir), using core.storage if available."""
    try:
        from core.storage import clips_dir, thumbs_dir  # type: ignore
        return Path(clips_dir()), Path(thumbs_dir())
    except Exception:
        pass

    storage_root = os.environ.get("STORAGE_DIR", "")
    if storage_root:
        root = Path(storage_root)
        return root / "clips", root / "thumbs"

    # Last resort: write alongside workdir
    return workdir, workdir


# ---------------------------------------------------------------------------
# Empty ASS fallback
# ---------------------------------------------------------------------------

def _empty_ass(resolution: list[int]) -> str:
    """Minimal valid empty ASS file (no subtitle events)."""
    out_w, out_h = resolution
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {out_w}\n"
        f"PlayResY: {out_h}\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,80,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,1,6,2,8,10,10,730,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


# ---------------------------------------------------------------------------
# Intermediate cleanup
# ---------------------------------------------------------------------------

def _cleanup_intermediates(workdir: Path, keep: set[Path]) -> None:
    """Remove all files in *workdir* except those in *keep*."""
    for item in workdir.iterdir():
        if item.is_file() and item.resolve() not in {p.resolve() for p in keep}:
            try:
                item.unlink()
            except Exception as exc:
                log.debug("_cleanup_intermediates: could not remove %s: %s", item, exc)
        elif item.is_dir() and item.name == "sample_frames":
            try:
                shutil.rmtree(item)
            except Exception:
                pass
