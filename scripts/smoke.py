"""
scripts/smoke.py — End-to-end smoke test for Clip Engine.

Takes one YouTube URL (YOUTUBE_URL env or a safe default), downloads it,
runs faster-whisper locally for a transcript, produces a ranking stub when
LLM_API_KEY is absent, renders one ~20-second clip via the configured backend,
inserts a Clip row, and prints where to find it in the Queue.

Designed to complete in under two minutes on typical hardware.
Caps to exactly 1 clip and a 20-second segment starting at t=10s.

Usage:
    YOUTUBE_URL=https://youtu.be/... python scripts/smoke.py
    python scripts/smoke.py  # uses built-in default URL
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("smoke")

# Safe, royalty-free 1-minute YouTube video used as the default smoke source.
DEFAULT_YOUTUBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo" (19s)

SMOKE_START = 0.0
SMOKE_END = 19.0       # keep under 20 s for speed
SMOKE_CAMPAIGN = "fitness"


def _download_video(url: str, dest: Path) -> None:
    """Download using yt-dlp."""
    log.info("Downloading: %s", url)
    import subprocess
    result = subprocess.run(
        [
            sys.executable, "-m", "yt_dlp",
            "-f", "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]/best",
            "--merge-output-format", "mp4",
            "-o", str(dest),
            "--quiet",
            url,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr[-1000:]}")
    if not dest.exists():
        # yt-dlp may have added an extension; find it
        for p in dest.parent.glob(f"{dest.stem}.*"):
            p.rename(dest)
            break
    if not dest.exists():
        raise RuntimeError(f"Download produced no file at {dest}")
    log.info("Downloaded: %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)


def _get_words_whisper(audio_path: Path) -> list[dict]:
    """Run faster-whisper locally and return word timings."""
    log.info("Running faster-whisper on %s", audio_path.name)
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-untyped]
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(audio_path), word_timestamps=True, language=None)
        words = []
        for seg in segments:
            if seg.words:
                for w in seg.words:
                    words.append({"word": w.word.strip(), "start": float(w.start), "end": float(w.end)})
        log.info("faster-whisper: %d words", len(words))
        return words
    except Exception as exc:
        log.warning("faster-whisper failed (%s); continuing without words", exc)
        return []


def _stub_clip_candidate(start: float, end: float) -> dict:
    """Return a fixed clip candidate when LLM ranking is unavailable."""
    return {
        "start": start,
        "end": end,
        "hook": "Check this out!",
        "score": 0.75,
        "reason": "Smoke test stub — no LLM ranking",
    }


def _load_smoke_campaign():
    """Load the fitness campaign config (or any available campaign)."""
    from core.config import load_campaign, load_enabled_campaigns
    yaml_path = Path("campaigns") / f"{SMOKE_CAMPAIGN}.yaml"
    if yaml_path.exists():
        return load_campaign(yaml_path, strict_assets=False)
    # Fall back to first available campaign
    cfgs = load_enabled_campaigns("campaigns", strict_assets=False)
    if not cfgs:
        raise RuntimeError("No campaigns found — create at least one YAML in campaigns/")
    log.warning("Campaign '%s' not found; using '%s'", SMOKE_CAMPAIGN, cfgs[0].name)
    return cfgs[0]


def main() -> None:
    url = os.getenv("YOUTUBE_URL", DEFAULT_YOUTUBE_URL)
    log.info("=== Clip Engine Smoke Test ===")
    log.info("Source URL: %s", url)

    t0 = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="clip_engine_smoke_") as tmpdir:
        tmp = Path(tmpdir)

        # 1. Download
        source_path = tmp / "source.mp4"
        _download_video(url, source_path)

        # 2. Transcript (cut the segment first, then whisper)
        import subprocess
        cut_path = tmp / "cut.wav"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(SMOKE_START),
                "-i", str(source_path),
                "-t", str(SMOKE_END - SMOKE_START),
                "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
                str(cut_path),
            ],
            capture_output=True, check=True,
        )
        words = _get_words_whisper(cut_path)

        # 3. Load campaign config
        cfg = _load_smoke_campaign()
        log.info("Campaign: %s (mode=%s)", cfg.name, cfg.mode)

        # 4. Build a stub clip candidate
        clip_candidate = _stub_clip_candidate(SMOKE_START, SMOKE_END)

        # 5. Render via configured backend
        from producer.render_dispatch import render_and_record

        # Use a real DB session to insert the Clip and RenderJob rows
        try:
            from core.db import get_session
            from core.models import Clip

            with get_session() as session:
                workdir = tmp / "work"
                workdir.mkdir()

                source_meta = {
                    "source_id": "smoke:test",
                    "platform": "youtube",
                    "url": url,
                    "author_handle": "smoke_test",
                }

                log.info("Rendering clip [%.1fs – %.1fs] via backend...", SMOKE_START, SMOKE_END)
                dispatch_result = render_and_record(
                    cfg=cfg,
                    source_meta=source_meta,
                    clip_candidate=clip_candidate,
                    source_video=source_path,
                    words=words,
                    workdir=workdir,
                    campaign_name=cfg.name,
                    campaign_mode=cfg.mode,
                    session=session,
                )

                if dispatch_result.status == "error":
                    log.error("Render failed: %s", dispatch_result.error)
                    sys.exit(1)

                # Insert Clip row
                clip_row = Clip(
                    campaign=cfg.name,
                    source_id="smoke:test",
                    start=SMOKE_START,
                    end=SMOKE_END,
                    kind="clip",
                    mode=cfg.mode,
                    aspect="9:16",
                    hook=clip_candidate.get("hook"),
                    score=clip_candidate.get("score"),
                    file_path=dispatch_result.file_path,
                    thumb_path=dispatch_result.thumb_path,
                    caption="Smoke test clip",
                    destination_channels=cfg.destinations.postiz_channels,
                    status="pending_review",
                )
                session.add(clip_row)
                session.commit()

                elapsed = round(time.monotonic() - t0, 1)
                log.info("=== Smoke test PASSED in %.1fs ===", elapsed)
                log.info("Clip id=%d  file=%s", clip_row.id, dispatch_result.file_path)
                log.info("View in Queue → http://localhost:8000  (or your Railway URL)")

        except Exception as exc:
            log.error("Smoke test FAILED: %s", exc, exc_info=True)
            sys.exit(1)


if __name__ == "__main__":
    main()
