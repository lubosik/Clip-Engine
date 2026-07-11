"""
producer/render_dispatch.py — Render backend selection, dispatch, and RenderJob recording.

Backend selection (RENDER_BACKEND env):
  'modal'  — require Modal token; fail loudly if missing.
  'local'  — always use in-process ffmpeg (current producer.render path).
  'auto'   — Modal when MODAL_TOKEN_ID + MODAL_TOKEN_SECRET (or ~/.modal.toml)
              are present AND R2 is configured; otherwise local.

After every render (modal or local) a RenderJob row is inserted so the
/api/spend endpoint and the --max-modal-spend guard have data to work with.
RenderJob.clip_id is set to None here; the caller (producer/run.py) creates
the Clip row after this call.

Spend guards:
  estimate_modal_batch_cost(n, session)
      → n × avg cost of last 20 OK modal render_jobs (fallback $0.03/clip)
  month_to_date_modal_spend(session)
      → sum of cost_estimate for OK modal jobs in the current calendar month
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Approximate Apify cost per discovered item (USD).
# Used for the --max-apify-spend guard.  This is a rough estimate documented
# in the code rather than a hard billing figure.
APIFY_COST_PER_ITEM: float = 0.01  # $0.01 / discovery item

# Default per-clip cost estimate when no historical render_jobs exist.
MODAL_DEFAULT_COST_PER_CLIP: float = 0.03  # $0.03 fallback


# ---------------------------------------------------------------------------
# Dispatch result
# ---------------------------------------------------------------------------

@dataclass
class DispatchResult:
    """Outcome of a single render_and_record call."""
    file_path: str        # local abs path or "r2://{key}"
    thumb_path: str       # local abs path or "r2://{key}"
    backend: str          # 'modal' | 'local'
    gpu: str | None       # e.g. 'l4', 't4', None for local
    duration_s: float
    status: str           # 'ok' | 'error'
    error: str | None


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def select_backend() -> str:
    """Return 'modal' or 'local' based on RENDER_BACKEND and credentials.

    Raises RuntimeError when RENDER_BACKEND='modal' but credentials are absent.
    """
    from core.settings import get_settings
    s = get_settings()
    backend = (s.render_backend or "auto").lower().strip()

    if backend == "local":
        log.debug("render_dispatch: backend=local (forced)")
        return "local"

    if backend == "modal":
        if not _modal_credentials_present(s):
            raise RuntimeError(
                "RENDER_BACKEND=modal but no Modal credentials found. "
                "Set MODAL_TOKEN_ID + MODAL_TOKEN_SECRET in your environment, "
                "or run 'modal token new' to authenticate via ~/.modal.toml."
            )
        log.debug("render_dispatch: backend=modal (forced)")
        return "modal"

    # auto
    if _modal_credentials_present(s) and s.r2_enabled:
        log.debug("render_dispatch: backend=modal (auto-selected: creds+r2 present)")
        return "modal"

    log.debug("render_dispatch: backend=local (auto-selected: missing creds or r2)")
    return "local"


def _modal_credentials_present(s: Any) -> bool:
    if s.modal_token_id and s.modal_token_secret:
        return True
    toml_path = Path("~/.modal.toml").expanduser()
    return toml_path.exists()


# ---------------------------------------------------------------------------
# Campaign asset upload (run once per session per campaign)
# ---------------------------------------------------------------------------

# Track which campaigns have had assets uploaded this process run.
_ASSETS_UPLOADED_CACHE: set[str] = set()


def ensure_campaign_assets_on_r2(cfg: Any) -> dict[str, str | None]:
    """Upload campaign asset files to R2 (skips existing objects).

    Returns a dict of R2 keys:
        {"font": key|None, "watermark": key|None, "badge": key|None, "outro": key|None}
    """
    from core.settings import get_settings
    from core import r2
    from core.storage import r2_key_for_asset

    s = get_settings()
    if not s.r2_enabled:
        log.debug("ensure_campaign_assets_on_r2: R2 not configured; returning None keys")
        return {"font": None, "watermark": None, "badge": None, "outro": None}

    result: dict[str, str | None] = {}
    campaign = cfg.name
    tmpl = cfg.template

    assets_to_upload = {
        "font": getattr(tmpl.captions, "font", None),
        "watermark": getattr(tmpl.watermark, "image", None),
        "badge": getattr(tmpl.corner_badge, "image", None),
        "outro": getattr(tmpl.outro, "clip", None) if tmpl.outro.enabled else None,
    }

    for role, local_path in assets_to_upload.items():
        if not local_path:
            result[role] = None
            continue

        local_file = Path(local_path)
        if not local_file.exists():
            log.warning("Asset file not found: %s (%s) — skipping", local_path, role)
            result[role] = None
            continue

        key = r2_key_for_asset(campaign, local_file.name)
        cache_key = f"{campaign}:{key}"

        if cache_key in _ASSETS_UPLOADED_CACHE:
            result[role] = key
            continue

        try:
            # Always (re)upload once per process so the CURRENTLY DEPLOYED asset
            # is authoritative. The previous skip-if-exists left stale assets in
            # R2 (e.g. an old boxed logo) winning over a freshly deployed one —
            # early renders in a run picked up the stale file. The per-process
            # cache below still prevents redundant re-uploads within a run.
            log.info("Uploading campaign asset: %s → %s", local_file.name, key)
            r2.upload_file(local_file, key)
            _ASSETS_UPLOADED_CACHE.add(cache_key)
            result[role] = key
        except Exception as exc:
            log.error("Failed to upload asset %s: %s", local_file.name, exc)
            result[role] = None

    return result


def upload_source_to_r2(
    source_video: Path,
    campaign: str,
    source_id: str,
) -> str | None:
    """Upload the raw source video to R2 and return the R2 key.

    Returns None if R2 is not configured.
    Skips the upload if the object already exists.
    """
    from core.settings import get_settings
    from core import r2
    from core.storage import r2_key_for_raw

    if not get_settings().r2_enabled:
        return None

    key = r2_key_for_raw(campaign, source_id)
    try:
        if not r2.exists(key):
            log.info("Uploading source video to R2: %s → %s", source_video.name, key)
            r2.upload_file(source_video, key)
        else:
            log.debug("Source video already in R2: %s", key)
        return key
    except Exception as exc:
        log.error("Failed to upload source %s to R2: %s", source_video.name, exc)
        return None


# ---------------------------------------------------------------------------
# Job dict builder
# ---------------------------------------------------------------------------

def build_job_dict(
    cfg: Any,
    source_meta: dict,
    clip_candidate: dict,
    asset_r2_keys: dict,
    raw_r2_key: str | None,
    output_video_key: str,
    output_thumb_key: str,
) -> dict:
    """Build the job dict for the Modal render_clip function.

    asset_r2_keys: {"font": key|None, "watermark": key|None,
                    "badge": key|None, "outro": key|None}

    Conforms to REVAMP_CONTRACTS §4.
    """
    tmpl = cfg.template
    return {
        "clip_id": clip_candidate.get("id"),
        "campaign": cfg.name,
        "mode": cfg.mode,
        "source": {
            "r2_raw_key": raw_r2_key,
            "url": source_meta.get("url"),
        },
        "start": float(clip_candidate["start"]),
        "end": float(clip_candidate["end"]),
        "template": {
            "resolution": list(tmpl.resolution),
            "captions": {
                "font_key": asset_r2_keys.get("font"),
                "base_color": tmpl.captions.base_color,
                "highlight_color": tmpl.captions.highlight_color,
                "outline_color": tmpl.captions.outline_color,
                "outline_px": tmpl.captions.outline_px,
                "position": tmpl.captions.position,
                "max_words_per_line": tmpl.captions.max_words_per_line,
            },
            "hook": {
                "enabled": tmpl.hook.enabled,
                "show_seconds": list(tmpl.hook.show_seconds),
                "box_color": tmpl.hook.box_color,
            },
            "watermark": {
                "r2_key": asset_r2_keys.get("watermark"),
                "position": tmpl.watermark.position,
                "opacity": tmpl.watermark.opacity,
                "scale": tmpl.watermark.scale,
            },
            "corner_badge": {
                "r2_key": asset_r2_keys.get("badge"),
                "position": tmpl.corner_badge.position,
                "opacity": tmpl.corner_badge.opacity,
                "scale": tmpl.corner_badge.scale,
            },
            "outro": {
                "enabled": tmpl.outro.enabled,
                "r2_key": asset_r2_keys.get("outro"),
                "audio": tmpl.outro.audio,
            },
            "lower_third": {
                "show_source_handle": tmpl.lower_third.show_source_handle,
                "format": tmpl.lower_third.format,
            },
        },
        "hook": clip_candidate.get("hook", ""),
        "source_handle": (
            source_meta.get("author_handle")
            or source_meta.get("channelName")
            or ""
        ),
        "words": None,  # Modal worker runs faster-whisper for caption timing
        "asset_keys": {
            "font": asset_r2_keys.get("font"),
            "watermark": asset_r2_keys.get("watermark"),
            "badge": asset_r2_keys.get("badge"),
            "outro": asset_r2_keys.get("outro"),
        },
        "output": {
            "video_key": output_video_key,
            "thumb_key": output_thumb_key,
        },
    }


# ---------------------------------------------------------------------------
# Modal dispatch
# ---------------------------------------------------------------------------

def dispatch_modal(job: dict) -> dict:
    """Dispatch a single render job to Modal and wait for the result.

    Uses modal.Function.from_name so this can be called without importing
    the modal_app module directly (avoids re-defining the image on the caller
    side).

    Returns the result dict from render_clip: {status, video_key, thumb_key,
    gpu, duration_s, error}.
    """
    try:
        import modal  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "Modal SDK is not installed. "
            "Install it with: pip install modal"
        ) from exc

    fn = modal.Function.from_name("clip-engine-render", "render_clip")
    log.info(
        "dispatch_modal: dispatching clip_id=%s campaign=%s [%.1fs–%.1fs]",
        job.get("clip_id"), job.get("campaign"), job.get("start"), job.get("end"),
    )
    result: dict = fn.remote(job)
    return result


def dispatch_modal_batch(jobs: list[dict]) -> list[dict]:
    """Dispatch a batch of render jobs concurrently using Modal .spawn().

    Spawns all jobs simultaneously, then gathers results in order.
    Returns results in the same order as the input jobs.
    """
    if not jobs:
        return []

    try:
        import modal  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("Modal SDK not installed") from exc

    fn = modal.Function.from_name("clip-engine-render", "render_clip")
    log.info("dispatch_modal_batch: spawning %d jobs", len(jobs))

    calls = [fn.spawn(job) for job in jobs]
    results = []
    for i, call in enumerate(calls):
        try:
            res = call.get()
            results.append(res)
        except Exception as exc:
            log.error("Batch job %d failed: %s", i, exc)
            results.append({
                "status": "error",
                "video_key": None,
                "thumb_key": None,
                "gpu": None,
                "duration_s": 0.0,
                "error": str(exc),
            })
    return results


# ---------------------------------------------------------------------------
# RenderJob insertion
# ---------------------------------------------------------------------------

def _insert_render_job(
    session: Any,
    *,
    campaign: str,
    backend: str,
    gpu: str | None,
    duration_s: float,
    status: str,
    error: str | None,
) -> None:
    """Insert a RenderJob row.  Does NOT commit (caller owns the transaction)."""
    from core.models import RenderJob
    from core.modal_costs import rate_for, estimate_cost

    rate = rate_for(gpu) if backend == "modal" else 0.0
    cost = estimate_cost(gpu, duration_s) if backend == "modal" else 0.0

    job_row = RenderJob(
        clip_id=None,   # Clip is created by the caller after this call
        campaign=campaign,
        backend=backend,
        gpu=gpu,
        duration_s=duration_s,
        rate_per_s=rate,
        cost_estimate=cost,
        status=status,
        error=error,
    )
    session.add(job_row)
    log.debug(
        "_insert_render_job: campaign=%s backend=%s gpu=%s cost=%.5f status=%s",
        campaign, backend, gpu, cost, status,
    )


# ---------------------------------------------------------------------------
# Main dispatch function
# ---------------------------------------------------------------------------

def render_and_record(
    cfg: Any,
    source_meta: dict,
    clip_candidate: dict,
    source_video: Path,
    words: list[dict] | None,
    workdir: Path,
    *,
    campaign_name: str,
    campaign_mode: str,
    session: Any,
) -> DispatchResult:
    """Render a clip, upload to R2 if configured, record RenderJob in DB.

    Selects the backend (modal/local), executes the render, inserts a
    RenderJob row into *session* (without committing — the caller commits).

    Returns:
        DispatchResult with file_path and thumb_path as "r2://{key}" when
        R2 is enabled, or local absolute paths for the local backend.

    Raises:
        RuntimeError on fatal errors (e.g. missing Modal credentials when
        backend=modal forced, or the local render fails hard).
    """
    backend = select_backend()
    t0 = time.monotonic()

    if backend == "modal":
        result = _render_modal(cfg, source_meta, clip_candidate, source_video, campaign_name)
    else:
        result = _render_local(cfg, source_meta, clip_candidate, source_video, words, workdir)

    duration_s = time.monotonic() - t0

    _insert_render_job(
        session,
        campaign=campaign_name,
        backend=result.backend,
        gpu=result.gpu,
        duration_s=result.duration_s if result.duration_s > 0 else duration_s,
        status=result.status,
        error=result.error,
    )

    return result


def _render_modal(
    cfg: Any,
    source_meta: dict,
    clip_candidate: dict,
    source_video: Path,
    campaign_name: str,
) -> DispatchResult:
    """Modal render path: upload source + assets to R2, dispatch, return R2 paths."""
    from core.storage import r2_key_for_clip, r2_key_for_thumb
    import uuid

    clip_uid = str(uuid.uuid4())[:12]
    output_video_key = r2_key_for_clip(campaign_name, clip_uid)
    output_thumb_key = r2_key_for_thumb(campaign_name, clip_uid)

    # Upload source to R2
    source_id = source_meta.get("source_id", clip_uid)
    raw_r2_key = upload_source_to_r2(source_video, campaign_name, source_id)

    # Ensure campaign assets are in R2
    asset_r2_keys = ensure_campaign_assets_on_r2(cfg)

    # Build and dispatch job
    job = build_job_dict(
        cfg=cfg,
        source_meta=source_meta,
        clip_candidate=clip_candidate,
        asset_r2_keys=asset_r2_keys,
        raw_r2_key=raw_r2_key,
        output_video_key=output_video_key,
        output_thumb_key=output_thumb_key,
    )

    modal_result = dispatch_modal(job)

    status = modal_result.get("status", "error")
    gpu = modal_result.get("gpu")
    duration_s = float(modal_result.get("duration_s", 0.0))
    error = modal_result.get("error")

    if status == "ok":
        file_path = f"r2://{modal_result['video_key']}"
        thumb_path = f"r2://{modal_result['thumb_key']}"
    else:
        log.error("Modal render failed: %s", error)
        file_path = ""
        thumb_path = ""

    return DispatchResult(
        file_path=file_path,
        thumb_path=thumb_path,
        backend="modal",
        gpu=gpu,
        duration_s=duration_s,
        status=status,
        error=error,
    )


def _render_local(
    cfg: Any,
    source_meta: dict,
    clip_candidate: dict,
    source_video: Path,
    words: list[dict] | None,
    workdir: Path,
) -> DispatchResult:
    """Local render path: call producer.render.render_clip in-process.

    If R2 is configured, upload the finished mp4 + thumbnail to R2 and
    delete the local copies (VPS should hold no video when R2 is configured).
    Returns r2:// paths when R2 is used, local paths otherwise.
    """
    from producer.render import render_clip, RenderResult

    t0 = time.monotonic()
    error: str | None = None

    try:
        result: RenderResult = render_clip(
            cfg=cfg,
            source_meta=source_meta,
            clip=clip_candidate,
            source_video=source_video,
            words=words,
            workdir=workdir,
        )
        duration_s = time.monotonic() - t0
        status = "ok"
    except Exception as exc:
        duration_s = time.monotonic() - t0
        log.error("Local render failed: %s", exc, exc_info=True)
        error = str(exc)
        return DispatchResult(
            file_path="",
            thumb_path="",
            backend="local",
            gpu=None,
            duration_s=duration_s,
            status="error",
            error=error,
        )

    file_path = str(result.final_path)
    thumb_path = str(result.thumb_path)

    # If R2 is enabled: upload and delete local copies
    from core.settings import get_settings
    if get_settings().r2_enabled:
        from core import r2
        from core.storage import r2_key_for_clip, r2_key_for_thumb
        import uuid
        clip_uid = result.final_path.stem  # already unique from the render
        campaign = cfg.name
        video_key = r2_key_for_clip(campaign, clip_uid)
        thumb_key = r2_key_for_thumb(campaign, clip_uid)
        try:
            r2.upload_file(result.final_path, video_key)
            r2.upload_file(result.thumb_path, thumb_key)
            # Delete local copies after successful upload
            result.final_path.unlink(missing_ok=True)
            result.thumb_path.unlink(missing_ok=True)
            file_path = f"r2://{video_key}"
            thumb_path = f"r2://{thumb_key}"
            log.info("Uploaded local render to R2 and deleted local copies")
        except Exception as exc:
            log.error("R2 upload after local render failed: %s — keeping local paths", exc)

    return DispatchResult(
        file_path=file_path,
        thumb_path=thumb_path,
        backend="local",
        gpu=None,
        duration_s=duration_s,
        status=status,
        error=error,
    )


# ---------------------------------------------------------------------------
# Spend helpers
# ---------------------------------------------------------------------------

def estimate_modal_batch_cost(n_clips: int, session: Any) -> float:
    """Estimate cost for rendering *n_clips* via Modal.

    Uses the average cost of the last 20 OK modal render_jobs as the
    per-clip estimate.  Falls back to MODAL_DEFAULT_COST_PER_CLIP ($0.03)
    when no history exists.
    """
    if n_clips <= 0:
        return 0.0

    avg = _avg_recent_modal_cost(session)
    return n_clips * avg


def _avg_recent_modal_cost(session: Any) -> float:
    """Return the average cost_estimate of the last 20 successful modal jobs."""
    try:
        from core.models import RenderJob
        rows = (
            session.query(RenderJob.cost_estimate)
            .filter(
                RenderJob.backend == "modal",
                RenderJob.status == "ok",
            )
            .order_by(RenderJob.created_at.desc())
            .limit(20)
            .all()
        )
        if not rows:
            return MODAL_DEFAULT_COST_PER_CLIP
        costs = [float(r[0]) for r in rows if r[0] is not None and r[0] > 0]
        return sum(costs) / len(costs) if costs else MODAL_DEFAULT_COST_PER_CLIP
    except Exception as exc:
        log.warning("Could not compute avg modal cost: %s", exc)
        return MODAL_DEFAULT_COST_PER_CLIP


def month_to_date_modal_spend(session: Any) -> float:
    """Return total estimated Modal spend for the current calendar month."""
    try:
        from core.models import RenderJob
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        rows = (
            session.query(RenderJob.cost_estimate)
            .filter(
                RenderJob.backend == "modal",
                RenderJob.status == "ok",
                RenderJob.created_at >= month_start,
            )
            .all()
        )
        return sum(float(r[0]) for r in rows if r[0] is not None)
    except Exception as exc:
        log.warning("Could not compute MTD modal spend: %s", exc)
        return 0.0
