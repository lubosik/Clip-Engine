"""
web/api.py — FastAPI application.

Implements every route in ARCHITECTURE §5 exactly.
Serves the static PWA from web/static/ at / (guarded: empty dir is fine).

Video streaming uses starlette FileResponse which handles HTTP Range natively.

Design decisions documented here:
- GET /api/stats next_run_at: derived by scanning enabled campaign schedules
  and computing the next cron slot that is in the future.  This is deterministic
  and requires no external state.
- POST /api/runs/{campaign}: spawns `python -m producer.run <campaign>` as a
  detached subprocess using the same Python interpreter that is running this
  process.  Output goes to a log file in STORAGE_DIR/logs/.
- CORS is not configured — the frontend is served from the same origin.
- All DB access goes through core.db.get_session().
- All responses are JSON-serialisable (datetime → ISO string, Path → str).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy import func as sa_func

from web.auth import require_auth
from web.campaigns_io import create_or_update_campaign, slugify

# ---------------------------------------------------------------------------
# Guarded core imports
# ---------------------------------------------------------------------------
try:
    from core.db import get_session
except Exception:
    get_session = None  # type: ignore[assignment]

try:
    from core.models import Analytics, Campaign, Clip, Source
except Exception:
    Analytics = None  # type: ignore[assignment]
    Campaign = None  # type: ignore[assignment]
    Clip = None  # type: ignore[assignment]
    Source = None  # type: ignore[assignment]

try:
    from core.config import load_campaign, load_enabled_campaigns
except Exception:
    load_campaign = None  # type: ignore[assignment]
    load_enabled_campaigns = None  # type: ignore[assignment]

try:
    from core.settings import settings as _settings
    STORAGE_DIR = Path(_settings.storage_dir)
except Exception:
    STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", "/data/clips"))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _db_required() -> None:
    """Raise 503 if the DB layer is not available."""
    if get_session is None or Clip is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "Database layer not available", "code": 503},
        )


app = FastAPI(
    title="Clip Engine API",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Helper: serialise a SQLAlchemy row to a plain dict
# ---------------------------------------------------------------------------

def _clip_to_dict(clip: Any, base_url: str = "") -> dict[str, Any]:
    """Convert a Clip ORM row to a JSON-serialisable dict per ARCHITECTURE §5."""
    source_info: dict[str, Any] = {}
    try:
        # Relationship may be named source_rel in some model versions.
        src = getattr(clip, "source_rel", None) or getattr(clip, "source", None)
        if src:
            # Column may be named source_metadata (reserved word fix) or metadata.
            meta = (
                getattr(src, "source_metadata", None)
                or getattr(src, "meta", None)
                or getattr(src, "metadata", None)
                or {}
            )
            if meta is None:
                meta = {}
            source_info = {
                "handle": (
                    meta.get("channelName")
                    or meta.get("authorMeta", {}).get("name")
                    or getattr(src, "author_handle", None)
                    or ""
                ),
                "url": getattr(src, "url", "") or "",
                "title": getattr(src, "title", "") or "",
                "platform": getattr(src, "platform", "") or "",
            }
    except Exception:
        pass

    created_at = clip.created_at
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at = created_at.isoformat()

    duration = None
    if clip.start is not None and clip.end is not None:
        duration = round(clip.end - clip.start, 2)

    clip_id = str(clip.id)
    return {
        "id": clip_id,
        "campaign": clip.campaign,
        "hook": clip.hook or "",
        "score": clip.score,
        "reason": clip.reason or "",
        "caption": clip.caption or "",
        "source": source_info,
        "start": clip.start,
        "end": clip.end,
        "duration": duration,
        "destination_channels": clip.destination_channels or [],
        "proposed_slot": (
            getattr(clip, "scheduled_at", None).isoformat()
            if getattr(clip, "scheduled_at", None)
            else None
        ),
        "created_at": created_at,
        "status": clip.status,
        "video_url": f"/api/clips/{clip_id}/video",
        "thumb_url": f"/api/clips/{clip_id}/thumb",
    }


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# /api/campaigns
# ---------------------------------------------------------------------------

@app.get("/api/campaigns", dependencies=[Depends(require_auth)])
def list_campaigns() -> list[dict[str, Any]]:
    """GET /api/campaigns — list all campaigns with summary info."""
    _db_required()

    results: list[dict[str, Any]] = []

    # Load from filesystem (source of truth for config).
    if load_enabled_campaigns is not None:
        try:
            cfgs = load_enabled_campaigns()
        except Exception as exc:
            logger.warning("Could not load campaign configs: %s", exc)
            cfgs = []
    else:
        cfgs = []

    # Load DB summaries.
    campaign_db: dict[str, Any] = {}
    try:
        with get_session() as session:
            rows = session.query(Campaign).all()
            for row in rows:
                campaign_db[row.name] = row
    except Exception as exc:
        logger.warning("Could not query campaigns table: %s", exc)

    pending_counts: dict[str, int] = {}
    try:
        with get_session() as session:
            rows = (
                session.query(Clip.campaign, sa_func.count(Clip.id))
                .filter(Clip.status == "pending_review")
                .group_by(Clip.campaign)
                .all()
            )
            for campaign_name, cnt in rows:
                pending_counts[campaign_name] = cnt
    except Exception as exc:
        logger.warning("Could not compute pending counts: %s", exc)

    for cfg in cfgs:
        name = cfg.name if hasattr(cfg, "name") else cfg["name"]
        enabled = cfg.enabled if hasattr(cfg, "enabled") else cfg.get("enabled", True)
        dest = cfg.destinations if hasattr(cfg, "destinations") else cfg.get("destinations", {})
        sched = dest.schedule if hasattr(dest, "schedule") else dest.get("schedule", {})

        db_row = campaign_db.get(name)
        last_run_at = None
        if db_row and db_row.updated_at:
            dt = db_row.updated_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            last_run_at = dt.isoformat()

        results.append({
            "name": name,
            "enabled": enabled,
            "sources_summary": _sources_summary(cfg),
            "schedule": {
                "posts_per_day": sched.posts_per_day if hasattr(sched, "posts_per_day") else sched.get("posts_per_day"),
                "times": sched.times if hasattr(sched, "times") else sched.get("times", []),
                "timezone": sched.timezone if hasattr(sched, "timezone") else sched.get("timezone", "UTC"),
            },
            "last_run_at": last_run_at,
            "pending_count": pending_counts.get(name, 0),
        })

    return results


def _sources_summary(cfg: Any) -> str:
    """Return a short human-readable summary of sources."""
    try:
        sources = cfg.sources if hasattr(cfg, "sources") else cfg.get("sources", {})
        parts = []
        for platform in ("youtube", "tiktok", "instagram"):
            ps = (
                getattr(sources, platform, None)
                or (sources.get(platform) if isinstance(sources, dict) else None)
            )
            if ps:
                terms = getattr(ps, "search_terms", None) or (ps.get("search_terms") if isinstance(ps, dict) else None) or []
                profiles = getattr(ps, "profiles", None) or (ps.get("profiles") if isinstance(ps, dict) else None) or []
                n = len(terms) + len(profiles)
                if n:
                    parts.append(f"{platform}({n})")
        return ", ".join(parts) if parts else "no sources"
    except Exception:
        return ""


@app.post("/api/campaigns", dependencies=[Depends(require_auth)])
async def create_campaign(
    config: str = Form(..., description="JSON string of campaign config"),
    logo: UploadFile | None = File(default=None),
    corner_badge: UploadFile | None = File(default=None),
    outro: UploadFile | None = File(default=None),
    font: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """POST /api/campaigns — create a new campaign from the wizard."""
    try:
        config_dict: dict[str, Any] = json.loads(config)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": f"Invalid JSON in config field: {exc}", "code": 422},
        )

    async def _read(upload: UploadFile | None) -> tuple[bytes | None, str]:
        if upload is None:
            return None, ""
        return await upload.read(), upload.filename or ""

    logo_bytes, logo_fn = await _read(logo)
    badge_bytes, badge_fn = await _read(corner_badge)
    outro_bytes, outro_fn = await _read(outro)
    font_bytes, font_fn = await _read(font)

    try:
        slug, yaml_path = create_or_update_campaign(
            config_dict,
            logo_bytes=logo_bytes,
            logo_filename=logo_fn,
            corner_badge_bytes=badge_bytes,
            corner_badge_filename=badge_fn,
            outro_bytes=outro_bytes,
            outro_filename=outro_fn,
            font_bytes=font_bytes,
            font_filename=font_fn,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": str(exc), "code": 422},
        )
    except Exception as exc:
        logger.error("Campaign create failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to save campaign", "code": 500},
        )

    # Return the saved config.
    if load_campaign is not None:
        try:
            saved_cfg = load_campaign(str(yaml_path))
            return saved_cfg.model_dump() if hasattr(saved_cfg, "model_dump") else dict(saved_cfg)
        except Exception:
            pass
    return {"name": slug, "yaml_path": str(yaml_path)}


@app.put("/api/campaigns/{name}", dependencies=[Depends(require_auth)])
async def update_campaign(
    name: str,
    config: str = Form(...),
    logo: UploadFile | None = File(default=None),
    corner_badge: UploadFile | None = File(default=None),
    outro: UploadFile | None = File(default=None),
    font: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """PUT /api/campaigns/{name} — update an existing campaign."""
    try:
        config_dict: dict[str, Any] = json.loads(config)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": f"Invalid JSON: {exc}", "code": 422},
        )
    # Force slug to match URL param.
    config_dict["name"] = slugify(name)

    async def _read(upload: UploadFile | None) -> tuple[bytes | None, str]:
        if upload is None:
            return None, ""
        return await upload.read(), upload.filename or ""

    logo_bytes, logo_fn = await _read(logo)
    badge_bytes, badge_fn = await _read(corner_badge)
    outro_bytes, outro_fn = await _read(outro)
    font_bytes, font_fn = await _read(font)

    try:
        slug, yaml_path = create_or_update_campaign(
            config_dict,
            logo_bytes=logo_bytes,
            logo_filename=logo_fn,
            corner_badge_bytes=badge_bytes,
            corner_badge_filename=badge_fn,
            outro_bytes=outro_bytes,
            outro_filename=outro_fn,
            font_bytes=font_bytes,
            font_filename=font_fn,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": str(exc), "code": 422},
        )
    except Exception as exc:
        logger.error("Campaign update failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to update campaign", "code": 500},
        )

    if load_campaign is not None:
        try:
            saved_cfg = load_campaign(str(yaml_path))
            return saved_cfg.model_dump() if hasattr(saved_cfg, "model_dump") else dict(saved_cfg)
        except Exception:
            pass
    return {"name": slug, "yaml_path": str(yaml_path)}


@app.get("/api/campaigns/{name}", dependencies=[Depends(require_auth)])
def get_campaign(name: str) -> dict[str, Any]:
    """GET /api/campaigns/{name} — return full campaign config as JSON."""
    campaigns_dir = Path(__file__).resolve().parent.parent / "campaigns"
    slug = slugify(name)
    yaml_path = campaigns_dir / f"{slug}.yaml"

    if not yaml_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": f"Campaign {name!r} not found", "code": 404},
        )

    if load_campaign is not None:
        try:
            cfg = load_campaign(str(yaml_path))
            return cfg.model_dump() if hasattr(cfg, "model_dump") else dict(cfg)
        except Exception as exc:
            logger.warning("Could not load campaign %r: %s", name, exc)

    # Fallback: raw YAML.
    import yaml
    with yaml_path.open() as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# /api/clips
# ---------------------------------------------------------------------------

@app.get("/api/clips", dependencies=[Depends(require_auth)])
def list_clips(
    status_filter: str | None = Query(default=None, alias="status"),
    campaign: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """GET /api/clips — paginated clip list with optional filters."""
    _db_required()

    try:
        with get_session() as session:
            q = session.query(Clip)
            if status_filter:
                q = q.filter(Clip.status == status_filter)
            if campaign:
                q = q.filter(Clip.campaign == campaign)
            q = q.order_by(Clip.created_at.desc()).offset(offset).limit(limit)
            clips = q.all()
            return [_clip_to_dict(c) for c in clips]
    except Exception as exc:
        logger.error("list_clips failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Database query failed", "code": 500},
        )


@app.get("/api/clips/{clip_id}/video", dependencies=[Depends(require_auth)])
def get_clip_video(clip_id: str) -> FileResponse:
    """GET /api/clips/{id}/video — serve mp4 with HTTP Range support.

    starlette's FileResponse handles Range headers natively.
    """
    _db_required()

    try:
        with get_session() as session:
            clip = session.get(Clip, clip_id)
    except Exception as exc:
        logger.error("get_clip_video DB error: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "DB error", "code": 500})

    if clip is None:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Clip {clip_id} not found", "code": 404},
        )
    if not clip.file_path:
        raise HTTPException(
            status_code=404,
            detail={"error": "Clip has no video file", "code": 404},
        )
    video_path = Path(clip.file_path)
    if not video_path.exists():
        raise HTTPException(
            status_code=404,
            detail={"error": "Video file not found on disk", "code": 404},
        )

    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=video_path.name,
    )


@app.get("/api/clips/{clip_id}/thumb", dependencies=[Depends(require_auth)])
def get_clip_thumb(clip_id: str) -> FileResponse:
    """GET /api/clips/{id}/thumb — serve thumbnail jpeg."""
    _db_required()

    try:
        with get_session() as session:
            clip = session.get(Clip, clip_id)
    except Exception as exc:
        logger.error("get_clip_thumb DB error: %s", exc)
        raise HTTPException(status_code=500, detail={"error": "DB error", "code": 500})

    if clip is None:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Clip {clip_id} not found", "code": 404},
        )

    # Derive thumb path from file_path if not stored separately.
    thumb_path: Path | None = None
    if hasattr(clip, "thumb_path") and clip.thumb_path:
        thumb_path = Path(clip.thumb_path)
    elif clip.file_path:
        # Convention: same directory, same stem, .jpg extension.
        fp = Path(clip.file_path)
        thumb_path = fp.parent / f"{fp.stem}_thumb.jpg"
        if not thumb_path.exists():
            # Try thumbs/ subdirectory.
            thumb_path = STORAGE_DIR / "thumbs" / f"{clip_id}.jpg"

    if thumb_path is None or not thumb_path.exists():
        raise HTTPException(
            status_code=404,
            detail={"error": "Thumbnail not found", "code": 404},
        )

    return FileResponse(
        path=str(thumb_path),
        media_type="image/jpeg",
    )


@app.post("/api/clips/{clip_id}/approve", dependencies=[Depends(require_auth)])
def approve_clip(
    clip_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /api/clips/{id}/approve — approve a clip, optionally overriding caption."""
    _db_required()

    try:
        with get_session() as session:
            clip = session.get(Clip, clip_id)
            if clip is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": f"Clip {clip_id} not found", "code": 404},
                )
            if body and isinstance(body, dict) and "caption" in body:
                clip.caption = body["caption"]
            clip.status = "approved"
            session.commit()
            return {"status": "approved", "id": clip_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("approve_clip failed clip=%s: %s", clip_id, exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to approve clip", "code": 500},
        )


@app.post("/api/clips/{clip_id}/reject", dependencies=[Depends(require_auth)])
def reject_clip(
    clip_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /api/clips/{id}/reject — reject a clip with optional reason."""
    _db_required()

    try:
        with get_session() as session:
            clip = session.get(Clip, clip_id)
            if clip is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": f"Clip {clip_id} not found", "code": 404},
                )
            clip.status = "rejected"
            if body and isinstance(body, dict):
                clip.reject_reason = body.get("reason", "")
            session.commit()
            return {"status": "rejected", "id": clip_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("reject_clip failed clip=%s: %s", clip_id, exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to reject clip", "code": 500},
        )


@app.patch("/api/clips/{clip_id}", dependencies=[Depends(require_auth)])
def patch_clip(clip_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """PATCH /api/clips/{id} — update caption (or other mutable fields)."""
    _db_required()

    try:
        with get_session() as session:
            clip = session.get(Clip, clip_id)
            if clip is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": f"Clip {clip_id} not found", "code": 404},
                )
            if "caption" in body:
                clip.caption = str(body["caption"])
            session.commit()
            return _clip_to_dict(clip)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("patch_clip failed clip=%s: %s", clip_id, exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to patch clip", "code": 500},
        )


# ---------------------------------------------------------------------------
# /api/analytics
# ---------------------------------------------------------------------------

@app.get("/api/analytics", dependencies=[Depends(require_auth)])
def get_analytics(
    campaign: str | None = Query(default=None),
    weeks: int = Query(default=8, ge=1, le=52),
) -> dict[str, Any]:
    """GET /api/analytics — weekly aggregated analytics per channel and per clip."""
    _db_required()

    from datetime import timedelta

    now = datetime.now(timezone.utc)
    since = now - timedelta(weeks=weeks)

    try:
        with get_session() as session:
            clips_q = session.query(Clip).filter(Clip.status == "posted")
            if campaign:
                clips_q = clips_q.filter(Clip.campaign == campaign)
            posted_clips = {str(c.id): c for c in clips_q.all()}

            analytics_q = (
                session.query(Analytics)
                .filter(
                    Analytics.clip_id.in_(list(posted_clips.keys())),
                    Analytics.pulled_at >= since.replace(tzinfo=None),
                )
                .order_by(Analytics.pulled_at.asc())
                .all()
            )
    except Exception as exc:
        logger.error("get_analytics DB error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Analytics query failed", "code": 500},
        )

    # Aggregate into weekly buckets per channel.
    from collections import defaultdict

    # channel -> week_start_str -> {views, likes, comments, shares, posts}
    channel_weekly: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"views": 0, "likes": 0, "comments": 0, "shares": 0, "posts": 0}
    ))

    clip_rows: list[dict[str, Any]] = []
    # Aggregate latest analytics per clip per platform.
    latest_per_clip: dict[str, dict[str, Any]] = {}

    for row in analytics_q:
        clip_id = str(row.clip_id)
        clip = posted_clips.get(clip_id)
        if not clip:
            continue

        pulled_at = row.pulled_at
        if pulled_at.tzinfo is None:
            pulled_at = pulled_at.replace(tzinfo=timezone.utc)

        # Week bucket: Monday of the week.
        week_start = pulled_at - timedelta(days=pulled_at.weekday())
        week_str = week_start.strftime("%Y-%m-%d")

        platform: str = row.platform or "unknown"
        # Map channel from clip's destination_channels.
        dest_channels: list[str] = clip.destination_channels or []
        channel = next(
            (ch for ch in dest_channels if platform in ch.lower()),
            platform,
        )

        ch_week = channel_weekly[channel][week_str]
        ch_week["views"] += row.views or 0
        ch_week["likes"] += row.likes or 0
        ch_week["comments"] += row.comments or 0
        ch_week["shares"] += row.shares or 0

        # Count unique clips per week.
        _posts_key = f"{clip_id}_{week_str}"
        if _posts_key not in ch_week:
            ch_week["posts"] += 1
            ch_week[_posts_key] = True  # marker

        # Track latest analytics per clip.
        key = f"{clip_id}_{platform}"
        existing = latest_per_clip.get(key)
        if existing is None or pulled_at > existing["_pulled_at"]:
            permalinks = clip.posted_permalinks or {}
            if isinstance(permalinks, str):
                try:
                    permalinks = json.loads(permalinks)
                except Exception:
                    permalinks = {}
            latest_per_clip[key] = {
                "_pulled_at": pulled_at,
                "clip_id": clip_id,
                "hook": clip.hook or "",
                "platform": platform,
                "permalink": permalinks.get(platform, ""),
                "views": row.views or 0,
                "likes": row.likes or 0,
                "comments": row.comments or 0,
                "shares": row.shares or 0,
                "posted_at": (
                    getattr(clip, "scheduled_at", None).isoformat()
                    if getattr(clip, "scheduled_at", None)
                    else None
                ),
            }

    # Serialise channel_weekly — remove marker keys.
    channels_out: list[dict[str, Any]] = []
    for channel, weeks_data in channel_weekly.items():
        weekly_list = []
        for week_start_str, metrics in sorted(weeks_data.items()):
            clean = {k: v for k, v in metrics.items() if not k.startswith("_") and not isinstance(v, bool)}
            clean["week_start"] = week_start_str
            weekly_list.append(clean)
        channels_out.append({"channel": channel, "weekly": weekly_list})

    clips_out = [
        {k: v for k, v in rec.items() if not k.startswith("_")}
        for rec in latest_per_clip.values()
    ]

    return {"channels": channels_out, "clips": clips_out}


# ---------------------------------------------------------------------------
# /api/stats
# ---------------------------------------------------------------------------

@app.get("/api/stats", dependencies=[Depends(require_auth)])
def get_stats() -> dict[str, Any]:
    """GET /api/stats — clip counts per status + next scheduled producer run time.

    next_run_at is computed by finding the earliest future posting slot across
    all enabled campaigns, then subtracting 2 hours (producer runs ahead of post
    time).  If no campaigns are configured, next_run_at is None.
    """
    _db_required()

    counts: dict[str, int] = {
        "pending": 0,
        "approved": 0,
        "scheduled": 0,
        "posted": 0,
    }

    try:
        with get_session() as session:
            for (st, cnt) in session.query(Clip.status, sa_func.count(Clip.id)).group_by(Clip.status).all():
                if st == "pending_review":
                    counts["pending"] = cnt
                elif st in counts:
                    counts[st] = cnt
    except Exception as exc:
        logger.warning("stats DB query failed: %s", exc)

    next_run_at: str | None = _compute_next_run_at()

    return {
        "pending": counts["pending"],
        "approved": counts["approved"],
        "scheduled": counts["scheduled"],
        "posted": counts["posted"],
        "next_run_at": next_run_at,
    }


def _compute_next_run_at() -> str | None:
    """Estimate the next producer cron run time.

    Looks at enabled campaign schedules.  The producer runs 2 hours before the
    earliest daily post slot.  Returns ISO string or None.
    """
    if load_enabled_campaigns is None:
        return None

    try:
        from datetime import timedelta

        from scheduler.schedule import compute_next_slot

        configs = load_enabled_campaigns()
        earliest: datetime | None = None
        for cfg in configs:
            try:
                slot = compute_next_slot(cfg, [])
                # Producer runs 2 hours before the post slot.
                run_time = slot - timedelta(hours=2)
                if earliest is None or run_time < earliest:
                    earliest = run_time
            except Exception:
                continue

        if earliest is not None:
            return earliest.isoformat()
    except Exception as exc:
        logger.debug("next_run_at computation failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# /api/runs
# ---------------------------------------------------------------------------

@app.post("/api/runs/{campaign}", dependencies=[Depends(require_auth)])
def trigger_run(campaign: str) -> dict[str, Any]:
    """POST /api/runs/{campaign} — spawn a producer run in the background.

    Uses the same Python interpreter that is running this process.
    Subprocess stdout/stderr are redirected to STORAGE_DIR/logs/producer-<campaign>.log.
    """
    # Validate campaign exists.
    slug = slugify(campaign)
    campaigns_dir = Path(__file__).resolve().parent.parent / "campaigns"
    yaml_path = campaigns_dir / f"{slug}.yaml"
    if not yaml_path.exists():
        raise HTTPException(
            status_code=404,
            detail={"error": f"Campaign {campaign!r} not found", "code": 404},
        )

    log_dir = STORAGE_DIR / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create log dir %s: %s", log_dir, exc)
        log_dir = Path("/tmp")

    log_path = log_dir / f"producer-{slug}.log"

    try:
        log_fh = log_path.open("a")
    except OSError:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "producer.run", slug],
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from the parent process group
        )
        logger.info(
            "Spawned producer run: campaign=%s pid=%d log=%s",
            slug,
            proc.pid,
            log_path,
        )
        return {"started": True, "campaign": slug, "pid": proc.pid}
    except Exception as exc:
        logger.error("Failed to spawn producer for %r: %s", slug, exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": f"Failed to start producer: {exc}", "code": 500},
        )


# ---------------------------------------------------------------------------
# Static PWA — mounted at / LAST so it never shadows the /api routes above.
# Guarded so an empty dir doesn't crash startup.
# ---------------------------------------------------------------------------

if _STATIC_DIR.exists() and any(_STATIC_DIR.iterdir()):
    app.mount(
        "/",
        StaticFiles(directory=str(_STATIC_DIR), html=True),
        name="static",
    )
    logger.info("Serving static PWA from %s", _STATIC_DIR)
else:
    logger.info(
        "web/static/ is empty or absent — static files will not be served yet"
    )

    @app.get("/", include_in_schema=False)
    def _root_placeholder() -> JSONResponse:
        return JSONResponse(
            {"message": "Clip Engine API. Frontend not yet deployed."},
            status_code=200,
        )
