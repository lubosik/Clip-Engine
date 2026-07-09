"""
render/modal_app.py — Modal serverless GPU worker for clip rendering.

Self-contained: NO imports from this repository.  Modal ships only this
single file into the container; all helpers are inlined here.

Pipeline
--------
1.  Source acquisition — pull from R2 (job["source"]["r2_raw_key"]) or
    download from URL (job["source"]["url"]).
2.  Asset downloads — font, watermark, badge, outro from R2 keys in
    job["asset_keys"]; any may be None (skipped gracefully).
3.  Cut + center-crop reframe (one ffmpeg pass):
    ``scale=-2:1920:flags=lanczos,crop=1080:1920``
    NOTE: face-aware reframing (mediapipe) is intentionally omitted from the
    GPU container — center-crop is used instead, consistent with §F of the
    master spec.
4.  WAV extraction (16-kHz mono) for faster-whisper.
5.  Caption timing — use job["words"] (source-relative) if provided;
    otherwise run faster-whisper "small" on the cut WAV.  Words are
    converted to cut-relative (subtract job["start"]) before ASS generation.
6.  ASS karaoke captions — word-by-word highlight, built from inlined logic
    adapted from producer/render/captions.py.
7.  Overlay pass (single ffmpeg filter_complex_script):
    - subtitles (ASS with fontsdir)
    - drawtext hook (enabled for job["template"]["hook"]["show_seconds"] window)
    - watermark overlay (center, opacity-controlled)
    - corner badge overlay (positioned by name)
    - "via @handle" credit drawtext at bottom
    Encode: h264_nvenc -preset p5 -cq 23; falls back to libx264 if nvenc
    is unavailable.
8.  Outro concat (re-encode with same codec) — if outro.enabled and
    outro.r2_key is set.
9.  Thumbnail — ffmpeg frame at 1 s (or mid-point for short clips), 480 px wide.
10. Upload mp4 + jpg to R2 at job["output"]["video_key"] / ["thumb_key"].
11. Return {status, video_key, thumb_key, gpu, duration_s, error}.

Never raises to the caller — catches all exceptions and returns error status.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import modal

# ---------------------------------------------------------------------------
# Modal image and app definition
# ---------------------------------------------------------------------------

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "fonts-dejavu-core")
    .pip_install(
        "boto3",
        "faster-whisper",
        "yt-dlp",
        # CUDA runtime libs so ctranslate2 can use the L4/T4 GPU for Whisper.
        # LD_LIBRARY_PATH is patched at runtime in _get_word_timings to point at
        # these pip-installed shared libraries.  The CPU fallback remains in place
        # so missing libs never crash the render.
        "nvidia-cublas-cu12",
        "nvidia-cudnn-cu12",
    )
)

app = modal.App("clip-engine-render", image=image)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@app.function(
    gpu=["l4", "t4", "any"],
    timeout=1800,
    secrets=[modal.Secret.from_name("clip-engine")],
)
def render_clip(job: dict) -> dict:
    """Render one clip on GPU and upload outputs to R2.

    See module docstring for full pipeline and job dict schema.

    Returns
    -------
    dict
        ``{"status": "ok"|"error", "video_key": str|None,
           "thumb_key": str|None, "gpu": str|None,
           "duration_s": float, "error": str|None}``
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("clip-engine.modal")
    t_start = time.monotonic()
    gpu_label = _detect_gpu()

    try:
        video_key, thumb_key = _run_pipeline(job, log)
        return {
            "status": "ok",
            "video_key": video_key,
            "thumb_key": thumb_key,
            "gpu": gpu_label,
            "duration_s": round(time.monotonic() - t_start, 2),
            "error": None,
        }
    except Exception as exc:
        log.error("render_clip pipeline failed: %s", exc, exc_info=True)
        return {
            "status": "error",
            "video_key": None,
            "thumb_key": None,
            "gpu": gpu_label,
            "duration_s": round(time.monotonic() - t_start, 2),
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _run_pipeline(job: dict, log: logging.Logger) -> tuple[str, str]:
    """Execute the full render pipeline.  Returns (video_key, thumb_key)."""
    workdir = Path(tempfile.mkdtemp(prefix="clip_engine_"))
    try:
        return _pipeline(job, workdir, log)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _pipeline(job: dict, workdir: Path, log: logging.Logger) -> tuple[str, str]:
    campaign: str = job["campaign"]
    start: float = float(job["start"])
    end: float = float(job["end"])
    duration: float = end - start
    tmpl: dict = job["template"]
    asset_keys: dict = job.get("asset_keys") or {}
    source_handle: str = job.get("source_handle") or ""
    hook_text: str = job.get("hook") or ""
    words_raw: list | None = job.get("words")
    output: dict = job["output"]

    log.info(
        "Pipeline start: campaign=%s clip_id=%s start=%.2fs end=%.2fs",
        campaign, job.get("clip_id"), start, end,
    )

    # -- 1. Download source -------------------------------------------------
    source_path = workdir / "source.mp4"
    _download_source(job["source"], source_path, log)

    # -- 2. Download assets ------------------------------------------------
    asset_dir = workdir / "assets"
    asset_dir.mkdir()
    font_path = _download_asset(asset_keys.get("font"), asset_dir / "font", log)
    wm_path = _download_asset(asset_keys.get("watermark"), asset_dir / "watermark.png", log)
    badge_path = _download_asset(asset_keys.get("badge"), asset_dir / "badge.png", log)
    outro_path = _download_asset(asset_keys.get("outro"), asset_dir / "outro.mp4", log)

    # -- 3. Cut + reframe --------------------------------------------------
    reframed = workdir / "reframed.mp4"
    audio_wav = workdir / "cut.wav"
    res = tmpl.get("resolution", [1080, 1920])
    out_w, out_h = int(res[0]), int(res[1])
    _cut_and_reframe(source_path, reframed, audio_wav, start, duration, out_w, out_h, log)

    # -- 4 & 5. Word timings ------------------------------------------------
    if words_raw:
        # Source-relative → cut-relative
        cut_words = _to_cut_relative(words_raw, start, end)
    else:
        log.info("No words provided; running faster-whisper on cut audio")
        try:
            cut_words = _get_word_timings(audio_wav, log)
        except Exception as exc:
            log.warning("faster-whisper failed (%s); captions will be blank", exc)
            cut_words = []

    # -- 6. ASS captions file ---------------------------------------------
    ass_path = workdir / "captions.ass"
    cap_cfg = tmpl.get("captions", {})
    font_name = _font_name_from_path(font_path) if font_path else "DejaVu Sans"
    _build_ass(cut_words, cap_cfg, out_w, out_h, font_name, ass_path)

    # -- 7. Overlay + encode -----------------------------------------------
    use_nvenc = _nvenc_available()
    codec_v = ["h264_nvenc", "-preset", "p5", "-cq", "23"] if use_nvenc else \
              ["libx264", "-preset", "fast", "-crf", "20"]
    log.info("Video codec: %s (nvenc=%s)", codec_v[0], use_nvenc)

    hook_cfg = tmpl.get("hook", {})
    wm_cfg = tmpl.get("watermark", {})
    badge_cfg = tmpl.get("corner_badge", {})
    lt_cfg = tmpl.get("lower_third", {})
    outro_cfg = tmpl.get("outro", {})

    overlaid = workdir / "overlaid.mp4"
    _apply_overlays(
        reframed, ass_path, hook_text, hook_cfg,
        wm_path, wm_cfg, badge_path, badge_cfg,
        source_handle, lt_cfg,
        font_path, out_w, out_h, codec_v, workdir, overlaid, log,
    )

    # -- 8. Outro concat ---------------------------------------------------
    if outro_cfg.get("enabled") and outro_path and outro_path.exists():
        final_mp4 = workdir / "final.mp4"
        _concat_outro(overlaid, outro_path, outro_cfg, codec_v, final_mp4, log,
                      out_w=out_w, out_h=out_h)
    else:
        final_mp4 = overlaid

    # -- 9. Thumbnail ------------------------------------------------------
    thumb_jpg = workdir / "thumb.jpg"
    _extract_thumbnail(final_mp4, thumb_jpg, log)

    # -- 10. Upload to R2 --------------------------------------------------
    video_key = output["video_key"]
    thumb_key = output["thumb_key"]
    _upload_to_r2(final_mp4, video_key, log)
    if thumb_jpg.exists():
        _upload_to_r2(thumb_jpg, thumb_key, log)
    else:
        log.warning("Thumbnail not produced; thumb_key will point to a missing object")

    log.info("Pipeline complete: video_key=%s thumb_key=%s", video_key, thumb_key)
    return video_key, thumb_key


# ---------------------------------------------------------------------------
# R2 helpers
# ---------------------------------------------------------------------------

def _r2_client() -> Any:
    import boto3  # type: ignore[import-untyped]
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _r2_bucket() -> str:
    return os.environ["R2_BUCKET"]


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------

def _detect_gpu() -> str | None:
    """Return normalised GPU label from nvidia-smi, or None if unavailable."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            name = r.stdout.strip().lower()
            if "l4" in name:
                return "l4"
            if "t4" in name:
                return "t4"
            if "a10" in name:
                return "a10g"
            return "any"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# nvenc availability
# ---------------------------------------------------------------------------

def _nvenc_available() -> bool:
    """Quick test: encode a tiny synthetic input with h264_nvenc."""
    r = subprocess.run(
        [
            "ffmpeg", "-hide_banner",
            "-f", "lavfi", "-i", "nullsrc=s=16x16:r=30",
            "-t", "0.1",
            "-c:v", "h264_nvenc",
            "-f", "null", "-",
        ],
        capture_output=True,
    )
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Source download
# ---------------------------------------------------------------------------

def _download_source(source: dict, dest: Path, log: logging.Logger) -> None:
    """Download the source video from R2 or URL."""
    r2_key = source.get("r2_raw_key")
    url = source.get("url")

    if r2_key:
        log.info("Downloading source from R2: %s", r2_key)
        _r2_client().download_file(_r2_bucket(), r2_key, str(dest))
        return

    if url:
        log.info("Downloading source from URL: %s", url)
        try:
            # Try yt-dlp first (handles YouTube, TikTok, etc.)
            import yt_dlp  # type: ignore[import-untyped]
            ydl_opts = {
                "outtmpl": str(dest),
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "quiet": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            if not dest.exists():
                raise RuntimeError("yt-dlp ran but output file missing")
        except Exception as ydl_exc:
            log.warning("yt-dlp failed (%s); trying urllib", ydl_exc)
            import urllib.request
            urllib.request.urlretrieve(url, str(dest))
        return

    raise ValueError("job['source'] must contain 'r2_raw_key' or 'url'")


# ---------------------------------------------------------------------------
# Asset download
# ---------------------------------------------------------------------------

def _download_asset(
    r2_key: str | None,
    dest: Path,
    log: logging.Logger,
) -> Path | None:
    """Download one asset from R2.  Returns dest path on success, None if key is absent."""
    if not r2_key:
        return None
    try:
        log.info("Downloading asset: %s → %s", r2_key, dest.name)
        # Preserve original extension from the R2 key
        original_ext = Path(r2_key).suffix
        if original_ext and dest.suffix != original_ext:
            dest = dest.with_suffix(original_ext)
        _r2_client().download_file(_r2_bucket(), r2_key, str(dest))
        return dest
    except Exception as exc:
        log.warning("Asset download failed (key=%s): %s — skipping", r2_key, exc)
        return None


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------

def _run_ffmpeg(cmd: list[str], desc: str) -> None:
    """Run an ffmpeg command, raising RuntimeError on non-zero exit."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed during '{desc}' (exit {result.returncode}): "
            f"{result.stderr[-2000:]}"
        )


def _cut_and_reframe(
    source: Path,
    out_video: Path,
    out_audio: Path,
    start: float,
    duration: float,
    out_w: int,
    out_h: int,
    log: logging.Logger,
) -> None:
    """Cut + center-crop reframe in one pass.

    Uses fast seek (-ss before -i) for speed.  Output timestamps are reset
    to t=0 via -avoid_negative_ts make_zero.  The scale/crop filter converts
    any input aspect ratio to out_w × out_h (9:16) by scaling to the target
    height and center-cropping the width.
    """
    # scale=-2:height maintains aspect ratio; crop to exact out_w×out_h.
    # setsar=1 resets the fractional sample-aspect-ratio that scale introduces
    # when the computed width isn't a perfect integer multiple of the height
    # (e.g. 3414×1920 → crop to 1080×1920 produces SAR 5120:5121 without this fix).
    # A non-unit SAR causes the ffmpeg concat filter to reject stream merging.
    scale_filter = (
        f"scale=-2:{out_h}:flags=lanczos,"
        f"crop={out_w}:{out_h},"
        f"setsar=1"
    )
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", str(source),
        "-t", str(duration),
        "-vf", scale_filter,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(out_video),
    ]
    log.info("Cut+reframe: start=%.2f duration=%.2f → %s", start, duration, out_video.name)
    _run_ffmpeg(cmd, "cut+reframe")

    # Extract 16-kHz mono WAV for faster-whisper
    wav_cmd = [
        "ffmpeg", "-y",
        "-i", str(out_video),
        "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        str(out_audio),
    ]
    try:
        _run_ffmpeg(wav_cmd, "extract-wav")
    except Exception as exc:
        log.warning("WAV extraction failed (%s); writing silent fallback", exc)
        _write_silent_wav(out_audio, duration)


def _write_silent_wav(path: Path, duration: float) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-t", str(duration),
        "-acodec", "pcm_s16le",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True)


# ---------------------------------------------------------------------------
# Word timings via faster-whisper
# ---------------------------------------------------------------------------

def _get_word_timings(audio_path: Path, log: logging.Logger) -> list[dict]:
    """Run faster-whisper 'small' on *audio_path* and return cut-relative word dicts.

    Tries CUDA first.  Both the WhisperModel constructor AND model.transcribe() are
    inside the same try block because ctranslate2 uses lazy CUDA initialisation: the
    constructor succeeds even when libcublas.so.12 is absent; the library-not-found
    error fires later, at transcribe() time.  Catching only the constructor (the old
    pattern) left the CPU fallback unreachable.

    Before constructing the model, attempts to extend LD_LIBRARY_PATH with the
    directories of pip-installed nvidia-cublas-cu12 and nvidia-cudnn-cu12 so that
    ctranslate2 can find the shared libraries without a root-level CUDA install.
    This block is fully guarded — its absence never crashes the render.

    Returns [] only if both CUDA and CPU paths fail; the caller treats [] as blank
    captions (still a valid render).
    """
    from faster_whisper import WhisperModel  # type: ignore[import-untyped]

    # Extend LD_LIBRARY_PATH so ctranslate2 finds pip-installed CUDA libs.
    try:
        import nvidia.cublas.lib as _cub
        import nvidia.cudnn.lib as _cud
        _nvidia_dirs = [str(Path(_cub.__file__).parent), str(Path(_cud.__file__).parent)]
        _existing_ldpath = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = ":".join(
            p for p in _nvidia_dirs + [_existing_ldpath] if p
        )
        log.info("faster-whisper: LD_LIBRARY_PATH extended with nvidia pip dirs: %s", _nvidia_dirs)
    except Exception:
        pass  # nvidia pip packages absent; CUDA path may still fail, CPU fallback will catch it

    log.info("faster-whisper: transcribing %s", audio_path.name)
    segments = None

    # CUDA attempt — constructor AND transcribe inside the same try so that lazy
    # CUDA init failures at transcribe() time are caught here, not silently skipped.
    try:
        model = WhisperModel("small", device="cuda", compute_type="float16")
        segments, _ = model.transcribe(str(audio_path), word_timestamps=True, language=None)
        log.info("faster-whisper: CUDA path succeeded")
    except Exception as cuda_exc:
        log.warning("faster-whisper CUDA failed (%s); retrying on CPU", cuda_exc)
        # CPU fallback — always available regardless of CUDA state.
        try:
            model = WhisperModel("small", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(str(audio_path), word_timestamps=True, language=None)
            log.info("faster-whisper: CPU fallback succeeded")
        except Exception as cpu_exc:
            log.warning(
                "faster-whisper CPU also failed (%s); captions will be blank", cpu_exc
            )
            return []

    words: list[dict] = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append({"word": w.word.strip(), "start": float(w.start), "end": float(w.end)})
    log.info("faster-whisper: %d words", len(words))
    return words


def _to_cut_relative(words: list[dict], clip_start: float, clip_end: float) -> list[dict]:
    """Convert source-relative words to cut-relative (t=0 = start of clip)."""
    result = []
    for w in words:
        ws = float(w["start"]) - clip_start
        we = float(w["end"]) - clip_start
        if we <= 0 or ws >= (clip_end - clip_start):
            continue
        ws = max(0.0, ws)
        we = min(clip_end - clip_start, we)
        if we > ws:
            result.append({"word": w["word"], "start": ws, "end": we})
    return result


# ---------------------------------------------------------------------------
# ASS caption generation (inlined from producer/render/captions.py)
# ---------------------------------------------------------------------------

def _build_ass(
    words: list[dict],
    cap_cfg: dict,
    out_w: int,
    out_h: int,
    font_name: str,
    output_path: Path,
) -> None:
    font_size = max(48, int(out_h * 0.042))
    base_color = _hex_to_ass_color(cap_cfg.get("base_color", "#FFFFFF"))
    highlight_color = _hex_to_ass_color(cap_cfg.get("highlight_color", "#00E5FF"))
    outline_color = _hex_to_ass_color(cap_cfg.get("outline_color", "#000000"))
    back_color = "&H00000000"
    outline_px = int(cap_cfg.get("outline_px", 6))
    position = cap_cfg.get("position", "upper_mid")
    max_wpl = int(cap_cfg.get("max_words_per_line", 4))

    alignment, margin_v = _position_to_alignment(position, out_h)
    lines_of_words = _group_into_lines(words, max_wpl)
    events = _build_dialogue_events(lines_of_words, base_color=base_color, highlight_color=highlight_color)
    content = _render_ass(
        play_res_x=out_w, play_res_y=out_h,
        font_name=font_name, font_size=font_size,
        primary_color=base_color, outline_color=outline_color,
        back_color=back_color, outline_px=outline_px,
        shadow_px=2, alignment=alignment, margin_v=margin_v,
        events=events,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8-sig")


def _hex_to_ass_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        aa = 0x00
    elif len(h) == 8:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        aa = 255 - int(h[6:8], 16)
    else:
        r, g, b, aa = 255, 255, 255, 0
    return f"&H{aa:02X}{b:02X}{g:02X}{r:02X}"


def _position_to_alignment(position: str, out_h: int) -> tuple[int, int]:
    pos = (position or "upper_mid").lower()
    if pos == "upper_mid":
        return 8, int(out_h * 0.38)
    if pos == "center":
        return 5, 0
    if pos == "lower_third":
        return 2, int(out_h * 0.15)
    if pos == "lower_mid":
        return 2, int(out_h * 0.28)
    if pos == "bottom":
        return 2, int(out_h * 0.08)
    return 8, int(out_h * 0.38)


def _group_into_lines(words: list[dict], max_wpl: int) -> list[list[dict]]:
    if not words:
        return []
    return [words[i:i + max_wpl] for i in range(0, len(words), max_wpl) if words[i:i + max_wpl]]


def _build_dialogue_events(
    lines: list[list[dict]],
    *,
    base_color: str,
    highlight_color: str,
) -> list[tuple[float, float, str]]:
    events: list[tuple[float, float, str]] = []
    for line_words in lines:
        for idx, word in enumerate(line_words):
            evt_start = word["start"]
            evt_end = line_words[idx + 1]["start"] if idx + 1 < len(line_words) else word["end"]
            if evt_end <= evt_start:
                evt_end = evt_start + 0.05
            text = _build_line_text(line_words, idx, base_color, highlight_color)
            events.append((evt_start, evt_end, text))
    return events


def _build_line_text(
    line_words: list[dict],
    active_idx: int,
    base_color: str,
    highlight_color: str,
) -> str:
    parts = []
    for i, w in enumerate(line_words):
        token = w["word"]
        if i == active_idx:
            parts.append(f"{{\\c{highlight_color}}}{token}{{\\c{base_color}}}")
        else:
            parts.append(token)
    return " ".join(parts)


def _render_ass(
    *,
    play_res_x: int,
    play_res_y: int,
    font_name: str,
    font_size: int,
    primary_color: str,
    outline_color: str,
    back_color: str,
    outline_px: int,
    shadow_px: int,
    alignment: int,
    margin_v: int,
    events: list[tuple[float, float, str]],
) -> str:
    style = (
        f"Style: Default,{font_name},{font_size},"
        f"{primary_color},&H000000FF,{outline_color},{back_color},"
        f"-1,0,0,0,100,100,0,0,1,{outline_px},{shadow_px},"
        f"{alignment},10,10,{margin_v},1"
    )
    evt_lines = [
        f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Default,,0,0,0,,{t}"
        for s, e, t in events
    ]
    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        "Collisions: Normal",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        style,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *evt_lines,
        "",
    ])


def _ass_time(seconds: float) -> str:
    total_cs = max(0, int(round(seconds * 100)))
    h = total_cs // 360000
    total_cs -= h * 360000
    m = total_cs // 6000
    total_cs -= m * 6000
    s = total_cs // 100
    cs = total_cs % 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _font_name_from_path(font_path: Path | None) -> str:
    if not font_path or not font_path.exists():
        return "DejaVu Sans"
    stem = font_path.stem
    return re.sub(r"[-_]", " ", stem)


# ---------------------------------------------------------------------------
# Overlay + encode pass
# ---------------------------------------------------------------------------

_BADGE_POSITIONS = {
    "top_right":    ("W-w-W*0.03", "H*0.03"),
    "top_left":     ("W*0.03",     "H*0.03"),
    "bottom_right": ("W-w-W*0.03", "H-h-H*0.03"),
    "bottom_left":  ("W*0.03",     "H-h-H*0.03"),
    "center":       ("(W-w)/2",    "(H-h)/2"),
}


def _escape_fc_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _escape_drawtext(text: str) -> str:
    return (
        text
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _html_to_ffmpeg_color(html_color: str) -> str:
    h = html_color.lstrip("#")
    if len(h) == 6:
        return f"0x{h}FF"
    if len(h) == 8:
        return f"0x{h}"
    return "0x111111CC"


def _apply_overlays(
    reframed: Path,
    ass_path: Path,
    hook_text: str,
    hook_cfg: dict,
    wm_path: Path | None,
    wm_cfg: dict,
    badge_path: Path | None,
    badge_cfg: dict,
    source_handle: str,
    lt_cfg: dict,
    font_path: Path | None,
    out_w: int,
    out_h: int,
    codec_v: list[str],
    workdir: Path,
    output: Path,
    log: logging.Logger,
) -> None:
    """Build filtergraph and run the overlay + encode pass."""
    # Inputs list
    inputs: list[str] = ["-i", str(reframed)]
    wm_slot: int | None = None
    badge_slot: int | None = None

    wm_exists = wm_path is not None and wm_path.exists()
    badge_exists = badge_path is not None and badge_path.exists()

    if wm_exists:
        wm_slot = len(inputs) // 2
        inputs += ["-i", str(wm_path)]
    if badge_exists:
        badge_slot = len(inputs) // 2
        inputs += ["-i", str(badge_path)]

    # Filtergraph lines
    lines: list[str] = []
    cur = "[v0]"
    lines.append(f"[0:v]copy{cur}")

    # 1. Subtitles
    ass_esc = _escape_fc_path(str(ass_path.resolve()))
    sub_filter = f"subtitles='{ass_esc}'"
    if font_path and font_path.exists():
        fdir_esc = _escape_fc_path(str(font_path.parent.resolve()))
        sub_filter += f":fontsdir='{fdir_esc}'"
    lines.append(f"{cur}{sub_filter}[v1]")
    cur = "[v1]"
    v_idx = 1

    # 2. Hook drawtext
    hook_enabled = hook_cfg.get("enabled", True)
    ss = hook_cfg.get("show_seconds", [0, 8])
    hook_start = float(ss[0]) if ss else 0.0
    hook_end = float(ss[1]) if len(ss) > 1 else 8.0
    if hook_enabled and hook_text.strip():
        hook_txt_file = workdir / "hook.txt"
        # Wrap at 22 chars/line so individual lines stay within frame width.
        # Cap to 4 lines with a trailing ellipsis if the text wraps further.
        _hook_lines = _wrap_text(hook_text, 22).split("\n")
        _MAX_HOOK_LINES = 4
        if len(_hook_lines) > _MAX_HOOK_LINES:
            _hook_lines = _hook_lines[:_MAX_HOOK_LINES]
            if not _hook_lines[-1].endswith("..."):
                _hook_lines[-1] = _hook_lines[-1].rstrip() + "..."
        hook_txt_file.write_text("\n".join(_hook_lines), encoding="utf-8")
        # Fit fontsize: approximate Montserrat ExtraBold average advance ≈ 0.60 × fontsize.
        # Ensure the longest wrapped line stays within 92% of the frame width.
        _longest_line = max(len(l) for l in _hook_lines) if _hook_lines else 1
        _base_fs = max(44, int(out_h * 0.038))
        _fit_fs = int((out_w * 0.92) / (0.60 * max(_longest_line, 1)))
        hook_fontsize = max(min(_base_fs, _fit_fs), 32)
        hook_y = int(out_h * 0.08)
        box_color = _html_to_ffmpeg_color(hook_cfg.get("box_color", "#111111CC"))
        fontfile_part = ""
        if font_path and font_path.exists():
            fontfile_part = f":fontfile='{_escape_fc_path(str(font_path))}'"
        v_idx += 1
        nxt = f"[v{v_idx}]"
        hook_filter = (
            f"drawtext=textfile='{_escape_fc_path(str(hook_txt_file))}'"
            f"{fontfile_part}"
            f":fontsize={hook_fontsize}"
            f":fontcolor=white"
            f":box=1:boxcolor={box_color}:boxborderw=20"
            f":x=(w-text_w)/2:y={hook_y}"
            f":enable='between(t\\,{hook_start:.3f}\\,{hook_end:.3f})'"
        )
        lines.append(f"{cur}{hook_filter}{nxt}")
        cur = nxt

    # 3. Watermark overlay
    if wm_exists and wm_slot is not None:
        wm_opacity = float(wm_cfg.get("opacity", 0.18))
        wm_scale = float(wm_cfg.get("scale", 0.5))
        wm_w = int(out_w * wm_scale)
        v_idx += 1
        nxt = f"[v{v_idx}]"
        lines.append(f"[{wm_slot}:v]format=rgba,colorchannelmixer=aa={wm_opacity:.4f}[wm_a]")
        lines.append(f"[wm_a]scale={wm_w}:-1[wm_s]")
        lines.append(f"{cur}[wm_s]overlay=(W-w)/2:(H-h)/2{nxt}")
        cur = nxt

    # 4. Badge overlay
    if badge_exists and badge_slot is not None:
        badge_opacity = float(badge_cfg.get("opacity", 1.0))
        badge_scale = float(badge_cfg.get("scale", 0.12))
        badge_w = int(out_w * badge_scale)
        badge_pos = badge_cfg.get("position", "top_right")
        bx, by = _BADGE_POSITIONS.get(badge_pos, _BADGE_POSITIONS["top_right"])
        v_idx += 1
        nxt = f"[v{v_idx}]"
        lines.append(f"[{badge_slot}:v]format=rgba,colorchannelmixer=aa={badge_opacity:.4f}[bg_a]")
        lines.append(f"[bg_a]scale={badge_w}:-1[bg_s]")
        lines.append(f"{cur}[bg_s]overlay={bx}:{by}{nxt}")
        cur = nxt

    # 5. Credit drawtext ("via @handle")
    show_credit = lt_cfg.get("show_source_handle", True)
    credit_fmt = lt_cfg.get("format", "via @{source_handle}")
    if show_credit and source_handle:
        credit_text = credit_fmt.format(source_handle=source_handle)
        credit_esc = _escape_drawtext(credit_text)
        credit_fs = max(24, int(out_h * 0.018))
        fontfile_part = ""
        if font_path and font_path.exists():
            fontfile_part = f":fontfile='{_escape_fc_path(str(font_path))}'"
        v_idx += 1
        nxt = f"[v{v_idx}]"
        lines.append(
            f"{cur}drawtext=text='{credit_esc}'{fontfile_part}"
            f":fontsize={credit_fs}:fontcolor=white@0.75"
            f":x=(w-text_w)/2:y=h*0.88{nxt}"
        )
        cur = nxt

    lines.append(f"{cur}copy[out]")

    fg_script = workdir / "filtergraph.txt"
    fg_script.write_text(";\n".join(lines), encoding="utf-8")

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + [
            "-filter_complex_script", str(fg_script),
            "-map", "[out]",
            "-map", "0:a?",
            "-c:v"] + codec_v + [
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output),
        ]
    )
    log.info("Overlay+encode pass → %s", output.name)
    _run_ffmpeg(cmd, "overlay+encode")


def _wrap_text(text: str, chars_per_line: int = 32) -> str:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        add_len = len(word) + (1 if current else 0)
        if current_len + add_len > chars_per_line and current:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += add_len
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Outro concat
# ---------------------------------------------------------------------------

def _concat_outro(
    main_clip: Path,
    outro: Path,
    outro_cfg: dict,
    codec_v: list[str],
    output: Path,
    log: logging.Logger,
    out_w: int = 1080,
    out_h: int = 1920,
) -> None:
    """Concat main clip + outro using the ffmpeg concat FILTER (not the concat demuxer).

    The concat demuxer requires bit-identical stream parameters; when the outro has a
    different fps or timebase from the main clip (a common case — e.g. 30 fps outro vs
    23.976 fps main) it silently drops video frames or corrupts timestamps.

    The concat filter normalises both inputs before joining:
    - FPS-matches outro to the main clip's detected fps.
    - Scale/pads outro to out_w × out_h.
    - Resamples both audio streams to 48 kHz stereo.
    - Uses anullsrc to synthesise silence for an outro that has no audio track.
    """
    # Probe main clip fps (fallback to 24000/1001 = 23.976 if probe fails).
    _vprobe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(main_clip)],
        capture_output=True, text=True,
    )
    main_fps = "24000/1001"
    if _vprobe.returncode == 0:
        try:
            for _s in json.loads(_vprobe.stdout).get("streams", []):
                if _s.get("codec_type") == "video":
                    main_fps = _s.get("r_frame_rate", main_fps)
                    break
        except Exception:
            pass

    # Probe outro for audio track presence and video duration.
    _oprobe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(outro)],
        capture_output=True, text=True,
    )
    outro_has_audio = False
    outro_dur = 2.5  # fallback
    if _oprobe.returncode == 0:
        try:
            for _s in json.loads(_oprobe.stdout).get("streams", []):
                if _s.get("codec_type") == "audio":
                    outro_has_audio = True
                if _s.get("codec_type") == "video":
                    outro_dur = float(_s.get("duration", outro_dur))
        except Exception:
            pass

    audio_mode = outro_cfg.get("audio", "keep")
    mute = audio_mode == "mute"

    # Filter chains shared for all modes.
    # setsar=1 on both inputs ensures the concat filter sees identical pixel geometry
    # (the main clip may carry a fractional SAR artifact from the scale/crop reframe step).
    _v_main = f"[0:v]setsar=1,fps={main_fps}[v0]"
    _v_outro = f"[1:v]scale={out_w}:{out_h}:flags=lanczos,setsar=1,fps={main_fps}[v1]"

    if mute:
        filter_complex = f"{_v_main};{_v_outro};[v0][v1]concat=n=2:v=1:a=0[vout]"
        map_args = ["-map", "[vout]", "-an"]
        audio_enc: list[str] = []
    elif outro_has_audio:
        filter_complex = (
            f"{_v_main};"
            f"[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];"
            f"{_v_outro};"
            f"[1:a]aresample=48000,aformat=channel_layouts=stereo[a1];"
            f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]"
        )
        map_args = ["-map", "[vout]", "-map", "[aout]"]
        audio_enc = ["-c:a", "aac", "-b:a", "192k"]
    else:
        # Outro has no audio track — synthesise silence for that segment.
        filter_complex = (
            f"{_v_main};"
            f"[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];"
            f"{_v_outro};"
            f"anullsrc=r=48000:cl=stereo,atrim=duration={outro_dur:.4f},"
            f"aformat=channel_layouts=stereo[a1];"
            f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]"
        )
        map_args = ["-map", "[vout]", "-map", "[aout]"]
        audio_enc = ["-c:a", "aac", "-b:a", "192k"]

    cmd = (
        ["ffmpeg", "-y",
         "-i", str(main_clip),
         "-i", str(outro),
         "-filter_complex", filter_complex]
        + map_args
        + ["-c:v"] + codec_v
        + audio_enc
        + ["-movflags", "+faststart", str(output)]
    )
    log.info(
        "Outro concat (filter) → %s  main_fps=%s outro_audio=%s mute=%s",
        output.name, main_fps, outro_has_audio, mute,
    )
    _run_ffmpeg(cmd, "outro-concat")


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------

def _extract_thumbnail(video: Path, out_path: Path, log: logging.Logger) -> None:
    """Extract frame at 1 s (or 10% of duration), scale to 480 px wide."""
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(video)],
        capture_output=True, text=True,
    )
    try:
        dur = float(json.loads(probe.stdout).get("format", {}).get("duration", 2.0))
    except Exception:
        dur = 2.0

    seek_t = min(1.0, dur * 0.1)
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek_t),
        "-i", str(video),
        "-vframes", "1",
        "-vf", "scale=480:-2",
        "-q:v", "3",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log.warning("Thumbnail extraction failed (exit %d)", result.returncode)


# ---------------------------------------------------------------------------
# R2 upload
# ---------------------------------------------------------------------------

def _upload_to_r2(local_path: Path, key: str, log: logging.Logger) -> None:
    client = _r2_client()
    bucket = _r2_bucket()
    size = local_path.stat().st_size
    client.upload_file(str(local_path), bucket, key)
    log.info("Uploaded to R2: key=%s size=%d", key, size)
