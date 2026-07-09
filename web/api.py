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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml as _yaml

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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy import func as sa_func

from web.auth import SESSION_COOKIE, require_auth, session_token
from web.campaigns_io import create_or_update_campaign, slugify

# ---------------------------------------------------------------------------
# Guarded core imports
# ---------------------------------------------------------------------------
try:
    from core.db import get_session
except Exception:
    get_session = None  # type: ignore[assignment]

try:
    from core.models import Analytics, Campaign, Clip, MemeProfile, RenderJob, Source
except Exception:
    Analytics = None  # type: ignore[assignment]
    Campaign = None  # type: ignore[assignment]
    Clip = None  # type: ignore[assignment]
    MemeProfile = None  # type: ignore[assignment]
    RenderJob = None  # type: ignore[assignment]
    Source = None  # type: ignore[assignment]

try:
    from core.storage import media_ref_is_r2
except Exception:
    def media_ref_is_r2(path: str) -> bool:  # type: ignore[misc]
        return False

try:
    from core import r2 as _r2
except Exception:
    _r2 = None  # type: ignore[assignment]

try:
    from core.config import load_campaign, load_enabled_campaigns
except Exception:
    load_campaign = None  # type: ignore[assignment]
    load_enabled_campaigns = None  # type: ignore[assignment]

try:
    from core.settings import get_settings
    STORAGE_DIR = Path(get_settings().storage_dir)
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


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    """Unauthenticated liveness probe for platform health checks."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Helper: serialise a SQLAlchemy row to a plain dict
# ---------------------------------------------------------------------------

def _clip_to_dict(clip: Any, base_url: str = "") -> dict[str, Any]:
    """Convert a Clip ORM row to a JSON-serialisable dict per ARCHITECTURE §5.

    Gains kind, mode, aspect (revamp v2).  source_id / start / end may be
    None for meme clips — handled gracefully.
    """
    source_info: dict[str, Any] = {}
    try:
        # Relationship may be named source_rel in some model versions.
        src = getattr(clip, "source_rel", None) or getattr(clip, "source", None)
        if src:
            # Column is source_metadata ("metadata" is reserved on DeclarativeBase —
            # getattr(src, "metadata") would return the ORM MetaData object, not a dict).
            meta = getattr(src, "source_metadata", None) or getattr(src, "meta", None)
            if not isinstance(meta, dict):
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
        # Revamp v2 fields
        "kind": getattr(clip, "kind", "clip"),
        "mode": getattr(clip, "mode", "production"),
        "aspect": getattr(clip, "aspect", "9:16"),
        "meme_meta": getattr(clip, "meme_meta", None),
        # Core fields
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

        ppd = sched.posts_per_day if hasattr(sched, "posts_per_day") else sched.get("posts_per_day", 1)
        times = sched.times if hasattr(sched, "times") else sched.get("times", [])
        tz = sched.timezone if hasattr(sched, "timezone") else sched.get("timezone", "UTC")

        db_row = campaign_db.get(name)
        last_run_at = None
        if db_row and db_row.updated_at:
            dt = db_row.updated_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            last_run_at = dt.isoformat()

        # mode and engines from config (revamp v2)
        cfg_mode = getattr(cfg, "mode", None) or (cfg.get("mode") if isinstance(cfg, dict) else None) or "demo"
        cfg_engines_obj = getattr(cfg, "engines", None) or (cfg.get("engines") if isinstance(cfg, dict) else None)
        if cfg_engines_obj is not None:
            engines = {
                "clips": getattr(cfg_engines_obj, "clips", True) if not isinstance(cfg_engines_obj, dict) else cfg_engines_obj.get("clips", True),
                "memes": getattr(cfg_engines_obj, "memes", False) if not isinstance(cfg_engines_obj, dict) else cfg_engines_obj.get("memes", False),
            }
        else:
            engines = {"clips": True, "memes": False}

        results.append({
            "name": name,
            "enabled": enabled,
            "mode": cfg_mode,
            "engines": engines,
            "sources_summary": _sources_summary_list(cfg),
            "schedule": {
                "posts_per_day": ppd,
                "times": times,
                "timezone": tz,
                "label": _schedule_label(ppd, times, tz),
            },
            "last_run_at": last_run_at,
            "pending_count": pending_counts.get(name, 0),
        })

    return results


def _schedule_label(posts_per_day: int | None, times: list[str], timezone_str: str) -> str:
    """Format a schedule into a human-readable label.

    Examples:
        1 post, ["17:00"], "America/New_York" → "1×/day · 17:00 America/New_York"
        2 posts, ["09:00", "17:00"], "UTC"    → "2×/day · 09:00, 17:00 UTC"
    """
    try:
        ppd = int(posts_per_day or 1)
        times_str = ", ".join(times) if times else "—"
        return f"{ppd}×/day · {times_str} {timezone_str}".strip()
    except Exception:
        return ""


def _sources_summary_list(cfg: Any) -> list[dict[str, Any]]:
    """Return a list of source summaries per REVAMP_CONTRACTS §6.

    Shape: [{platform, count, label}]
    Label examples: "YouTube · 3 terms", "TikTok · 2 hashtags"
    """
    _PLATFORM_LABELS = {
        "youtube": "YouTube",
        "tiktok": "TikTok",
        "instagram": "Instagram",
    }
    result: list[dict[str, Any]] = []
    try:
        sources = cfg.sources if hasattr(cfg, "sources") else cfg.get("sources", {})
        for platform in ("youtube", "tiktok", "instagram"):
            ps = (
                getattr(sources, platform, None)
                or (sources.get(platform) if isinstance(sources, dict) else None)
            )
            if ps is None:
                continue
            terms = getattr(ps, "search_terms", None) or (ps.get("search_terms") if isinstance(ps, dict) else None) or []
            profiles = getattr(ps, "profiles", None) or (ps.get("profiles") if isinstance(ps, dict) else None) or []
            hashtags = getattr(ps, "hashtags", None) or (ps.get("hashtags") if isinstance(ps, dict) else None) or []
            channels = getattr(ps, "channels", None) or (ps.get("channels") if isinstance(ps, dict) else None) or []

            count = len(terms) + len(profiles) + len(hashtags) + len(channels)
            if count == 0:
                continue

            # Build a descriptive label
            pieces = []
            if terms:
                pieces.append(f"{len(terms)} term{'s' if len(terms) != 1 else ''}")
            if channels:
                pieces.append(f"{len(channels)} channel{'s' if len(channels) != 1 else ''}")
            if profiles:
                pieces.append(f"{len(profiles)} profile{'s' if len(profiles) != 1 else ''}")
            if hashtags:
                pieces.append(f"{len(hashtags)} hashtag{'s' if len(hashtags) != 1 else ''}")

            label = f"{_PLATFORM_LABELS.get(platform, platform.title())} · {', '.join(pieces)}"
            result.append({"platform": platform, "count": count, "label": label})
    except Exception:
        pass
    return result


@app.post("/api/campaigns", dependencies=[Depends(require_auth)])
async def create_campaign(
    config: str = Form(..., description="JSON string of campaign config"),
    logo: UploadFile | None = File(default=None),
    corner_badge: UploadFile | None = File(default=None),
    outro: UploadFile | None = File(default=None),
    font: UploadFile | None = File(default=None),
    meme_refs: list[UploadFile] = File(default=[]),
    visual_refs: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    """POST /api/campaigns — create a new campaign from the wizard.

    Accepts new revamp v2 fields in the config JSON:
        mode, engines, creative_direction, meme, demo.test_channels
    Accepts meme_refs as multiple image file uploads (saved to meme_refs dir).
    In R2 mode, uploads all assets to R2 under campaigns/{name}/assets/.
    """
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

    # Read meme ref files
    meme_refs_data: list[tuple[bytes, str]] = []
    for meme_ref in (meme_refs or []):
        data = await meme_ref.read()
        if data:
            meme_refs_data.append((data, meme_ref.filename or "ref.png"))

    # Read visual reference files (desired clip look — creative guidance)
    visual_refs_data: list[tuple[bytes, str]] = []
    for visual_ref in (visual_refs or []):
        data = await visual_ref.read()
        if data:
            visual_refs_data.append((data, visual_ref.filename or "ref.png"))

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
            meme_refs_files=meme_refs_data or None,
            visual_refs_files=visual_refs_data or None,
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
    meme_refs: list[UploadFile] = File(default=[]),
    visual_refs: list[UploadFile] = File(default=[]),
) -> dict[str, Any]:
    """PUT /api/campaigns/{name} — update an existing campaign.

    Accepts the same new fields as POST /api/campaigns.
    """
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

    meme_refs_data: list[tuple[bytes, str]] = []
    for meme_ref in (meme_refs or []):
        data = await meme_ref.read()
        if data:
            meme_refs_data.append((data, meme_ref.filename or "ref.png"))

    # Read visual reference files (desired clip look — creative guidance)
    visual_refs_data: list[tuple[bytes, str]] = []
    for visual_ref in (visual_refs or []):
        data = await visual_ref.read()
        if data:
            visual_refs_data.append((data, visual_ref.filename or "ref.png"))

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
            meme_refs_files=meme_refs_data or None,
            visual_refs_files=visual_refs_data or None,
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
    with yaml_path.open() as fh:
        return _yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# /api/campaigns/{name}/engines  and  /api/campaigns/{name}/mode  (PATCH)
# ---------------------------------------------------------------------------

def _campaigns_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "campaigns"


def _load_campaign_yaml(name: str) -> tuple[str, Path, dict[str, Any]]:
    """Load a campaign YAML by name.  Returns (slug, path, raw_dict).

    Raises HTTPException 404 if not found.
    """
    slug = slugify(name)
    yaml_path = _campaigns_dir() / f"{slug}.yaml"
    if not yaml_path.exists():
        raise HTTPException(
            status_code=404,
            detail={"error": f"Campaign {name!r} not found", "code": 404},
        )
    with yaml_path.open(encoding="utf-8") as fh:
        raw: dict[str, Any] = _yaml.safe_load(fh) or {}
    return slug, yaml_path, raw


def _write_campaign_yaml(yaml_path: Path, raw: dict[str, Any]) -> None:
    """Write raw dict back to the YAML file and update DB snapshot if possible."""
    with yaml_path.open("w", encoding="utf-8") as fh:
        _yaml.safe_dump(raw, fh, allow_unicode=True, sort_keys=False)

    # Best-effort: update config_snapshot in DB
    if get_session is not None and Campaign is not None:
        try:
            slug = yaml_path.stem
            with get_session() as session:
                row = session.query(Campaign).filter(Campaign.name == slug).first()
                if row is not None:
                    row.config_snapshot = raw
                    session.commit()
        except Exception as exc:
            logger.warning("Could not update config_snapshot for %s: %s", yaml_path.stem, exc)


@app.patch("/api/campaigns/{name}/engines", dependencies=[Depends(require_auth)])
def patch_campaign_engines(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """PATCH /api/campaigns/{name}/engines — toggle clip/meme engines on/off.

    Body: {clips?: bool, memes?: bool}
    Updates the YAML and refreshes Campaign.config_snapshot in the DB.
    """
    if not body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "Request body must include clips or memes", "code": 422},
        )
    for key in body:
        if key not in {"clips", "memes"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": f"Unknown engine key {key!r}; expected 'clips' or 'memes'", "code": 422},
            )
        if not isinstance(body[key], bool):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": f"Engine value for {key!r} must be a boolean", "code": 422},
            )

    slug, yaml_path, raw = _load_campaign_yaml(name)
    engines: dict[str, Any] = dict(raw.get("engines") or {"clips": True, "memes": False})
    engines.update(body)
    raw["engines"] = engines
    _write_campaign_yaml(yaml_path, raw)

    return {"name": slug, "engines": engines}


@app.patch("/api/campaigns/{name}/mode", dependencies=[Depends(require_auth)])
def patch_campaign_mode(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """PATCH /api/campaigns/{name}/mode — set campaign mode to demo or production.

    Body: {mode: "demo" | "production"}
    Updates the YAML and refreshes Campaign.config_snapshot in the DB.
    """
    new_mode = body.get("mode")
    if new_mode not in {"demo", "production"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "mode must be 'demo' or 'production'", "code": 422},
        )

    slug, yaml_path, raw = _load_campaign_yaml(name)
    raw["mode"] = new_mode
    _write_campaign_yaml(yaml_path, raw)

    return {"name": slug, "mode": new_mode}


# ---------------------------------------------------------------------------
# /api/spend
# ---------------------------------------------------------------------------

def _compute_spend_data(session: Any, months: int) -> dict[str, Any]:
    """Aggregate render_jobs into the spend response shape.

    Extracted as a standalone function so it can be unit-tested directly
    with an in-memory SQLite session.
    """
    from core.settings import get_settings

    budget_usd: float = get_settings().modal_monthly_budget

    since = datetime.now(timezone.utc) - timedelta(days=30 * months)

    jobs = (
        session.query(RenderJob)
        .filter(RenderJob.created_at >= since, RenderJob.status == "ok")
        .all()
    )

    mtd: float = sum(float(j.cost_estimate or 0) for j in jobs)
    remaining: float = max(0.0, budget_usd - mtd)

    # Aggregate by campaign
    by_campaign_raw: dict[str, dict[str, Any]] = {}
    for j in jobs:
        entry = by_campaign_raw.setdefault(j.campaign, {"campaign": j.campaign, "usd": 0.0, "jobs": 0})
        entry["usd"] += float(j.cost_estimate or 0)
        entry["jobs"] += 1

    # Round per-campaign USD
    by_campaign = [
        {**v, "usd": round(v["usd"], 6)}
        for v in sorted(by_campaign_raw.values(), key=lambda x: x["usd"], reverse=True)
    ]

    # Most recent 20 jobs (any status)
    recent_rows = (
        session.query(RenderJob)
        .order_by(RenderJob.created_at.desc())
        .limit(20)
        .all()
    )
    recent: list[dict[str, Any]] = []
    for j in recent_rows:
        created_str = j.created_at
        if isinstance(created_str, datetime):
            if created_str.tzinfo is None:
                created_str = created_str.replace(tzinfo=timezone.utc)
            created_str = created_str.isoformat()
        recent.append({
            "clip_id": j.clip_id,
            "campaign": j.campaign,
            "gpu": j.gpu,
            "duration_s": j.duration_s,
            "usd": round(float(j.cost_estimate or 0), 6),
            "created_at": created_str,
        })

    return {
        "estimated": True,
        "budget_usd": budget_usd,
        "month_to_date_usd": round(mtd, 6),
        "remaining_credit_usd": round(remaining, 6),
        "by_campaign": by_campaign,
        "recent": recent,
        "plan_note": (
            "Costs are estimates derived from recorded wall-clock duration × "
            "published Modal GPU rates (modal.com/pricing, verified 2026-07-08). "
            "Modal's billing API requires a Team or Enterprise plan — these "
            "figures are not reconciled against actual billing."
        ),
    }


@app.get("/api/spend", dependencies=[Depends(require_auth)])
def get_spend(months: int = Query(default=1, ge=1, le=12)) -> dict[str, Any]:
    """GET /api/spend?months=N — Modal spend summary from render_jobs ledger.

    Returns estimated costs; actual billing requires a Modal Team/Enterprise plan.
    """
    _db_required()

    if RenderJob is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "render_jobs table not available", "code": 503},
        )

    try:
        with get_session() as session:
            return _compute_spend_data(session, months)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("get_spend failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Spend query failed", "code": 500},
        )


# ---------------------------------------------------------------------------
# /api/hero  (unauthenticated — login page uses this before the user logs in)
# ---------------------------------------------------------------------------

# Simple in-process cache for the R2 existence check (~10 min TTL)
_hero_cache: dict[str, Any] = {}
_hero_cache_ts: float = 0.0
_HERO_CACHE_TTL: float = 600.0  # 10 minutes

_HERO_KEYS = {
    "video": "hero/hero_loop.mp4",
    "video_vertical": "hero/hero_loop_vertical.mp4",
    "poster": "hero/hero_poster_web.jpg",
    "poster_mobile": "hero/hero_poster_mobile.jpg",
}


@app.get("/api/hero", include_in_schema=True)
def get_hero() -> dict[str, Any]:
    """GET /api/hero — presigned URLs for hero assets (login page background).

    No auth required — the login page calls this before the user has a token.
    If R2 is enabled and the hero objects exist, returns presigned URLs.
    Otherwise returns null values and the frontend falls back to a CSS backdrop.

    Results are cached for 10 minutes to avoid hammering R2 HeadObject.
    """
    global _hero_cache, _hero_cache_ts

    now = time.monotonic()
    if _hero_cache and (now - _hero_cache_ts) < _HERO_CACHE_TTL:
        return _hero_cache

    result: dict[str, Any] = {k: None for k in _HERO_KEYS}

    try:
        from core.settings import get_settings as _gs
        if _gs().r2_enabled and _r2 is not None:
            for field, key in _HERO_KEYS.items():
                try:
                    if _r2.exists(key):
                        result[field] = _r2.presign(key)
                except Exception as exc:
                    logger.debug("Hero R2 check failed for key=%s: %s", key, exc)
    except Exception as exc:
        logger.debug("get_hero failed: %s", exc)

    _hero_cache = result
    _hero_cache_ts = now
    return result


# ---------------------------------------------------------------------------
# /api/clips
# ---------------------------------------------------------------------------

@app.get("/api/clips", dependencies=[Depends(require_auth)])
def list_clips(
    status_filter: str | None = Query(default=None, alias="status"),
    campaign: str | None = Query(default=None),
    kind: str | None = Query(default=None, description="Filter by kind: 'clip' or 'meme'"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """GET /api/clips — paginated clip list with optional filters.

    Supports ?kind=clip|meme to filter by content type (revamp v2).
    """
    _db_required()

    if kind is not None and kind not in {"clip", "meme"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "kind must be 'clip' or 'meme'", "code": 422},
        )

    try:
        with get_session() as session:
            q = session.query(Clip)
            if status_filter:
                q = q.filter(Clip.status == status_filter)
            if campaign:
                q = q.filter(Clip.campaign == campaign)
            if kind is not None and Clip is not None and hasattr(Clip, "kind"):
                q = q.filter(Clip.kind == kind)
            q = q.order_by(Clip.created_at.desc()).offset(offset).limit(limit)
            clips = q.all()
            return [_clip_to_dict(c) for c in clips]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("list_clips failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Database query failed", "code": 500},
        )


@app.get("/api/clips/{clip_id}/video", dependencies=[Depends(require_auth)])
def get_clip_video(clip_id: str) -> Any:
    """GET /api/clips/{id}/video — serve mp4 or redirect to presigned R2 URL.

    When file_path starts with 'r2://', returns a 307 redirect to a presigned
    URL so the browser streams directly from R2.  Otherwise streams from disk
    (starlette FileResponse handles Range headers natively).
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

    # R2 path: redirect to presigned URL
    if media_ref_is_r2(clip.file_path) and _r2 is not None:
        try:
            key = clip.file_path.removeprefix("r2://")
            url = _r2.presign(key)
            return RedirectResponse(url=url, status_code=307)
        except Exception as exc:
            logger.error("R2 presign failed for clip=%s: %s", clip_id, exc)
            raise HTTPException(
                status_code=502,
                detail={"error": "Failed to generate video URL", "code": 502},
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
def get_clip_thumb(clip_id: str) -> Any:
    """GET /api/clips/{id}/thumb — serve thumbnail or redirect to presigned R2 URL.

    When thumb_path starts with 'r2://', returns a 307 redirect to a presigned
    URL.  Otherwise serves the local file.
    """
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

    # R2 thumb path
    if hasattr(clip, "thumb_path") and clip.thumb_path and media_ref_is_r2(clip.thumb_path):
        if _r2 is not None:
            try:
                key = clip.thumb_path.removeprefix("r2://")
                url = _r2.presign(key)
                return RedirectResponse(url=url, status_code=307)
            except Exception as exc:
                logger.error("R2 presign for thumb failed clip=%s: %s", clip_id, exc)
                raise HTTPException(
                    status_code=502,
                    detail={"error": "Failed to generate thumbnail URL", "code": 502},
                )

    # Derive thumb path from file_path if not stored separately.
    thumb_path: Path | None = None
    if hasattr(clip, "thumb_path") and clip.thumb_path:
        thumb_path = Path(clip.thumb_path)
    elif clip.file_path and not media_ref_is_r2(clip.file_path):
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
                    Analytics.pulled_at >= since,
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

# On-demand web-triggered runs always carry a hard spend ceiling (spec §9).
# These mirror `make demo`'s conservative caps; the operator can raise them per
# request via the JSON body.  The uncapped path stays reserved for the cron
# (`producer.run --all`), which is bounded by campaign discovery limits instead.
DEFAULT_ONDEMAND_APIFY_SPEND = 2.0
DEFAULT_ONDEMAND_MODAL_SPEND = 2.0


@app.post("/api/runs/{campaign}", dependencies=[Depends(require_auth)])
def trigger_run(
    campaign: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """POST /api/runs/{campaign} — spawn a producer run in the background.

    Uses the same Python interpreter that is running this process.
    Subprocess stdout/stderr are redirected to STORAGE_DIR/logs/producer-<campaign>.log.

    Optional JSON body (all keys optional):
      - mode: "demo" | "production" — overrides the campaign's configured mode.
      - max_apify_spend: float USD ceiling for discovery (default 2.0).
      - max_modal_spend: float USD ceiling for render (default 2.0).
    A web-triggered run is NEVER uncapped: omitted caps fall back to the demo
    defaults so the spec §9 spend gate always applies.
    """
    body = body or {}

    # Validate optional mode override.
    run_mode = body.get("mode")
    if run_mode is not None and run_mode not in {"demo", "production"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "mode must be 'demo' or 'production'", "code": 422},
        )

    def _cap(key: str, default: float) -> float:
        raw = body.get(key)
        if raw is None:
            return default
        try:
            val = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": f"{key} must be a number", "code": 422},
            )
        if val <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": f"{key} must be greater than 0", "code": 422},
            )
        return val

    max_apify_spend = _cap("max_apify_spend", DEFAULT_ONDEMAND_APIFY_SPEND)
    max_modal_spend = _cap("max_modal_spend", DEFAULT_ONDEMAND_MODAL_SPEND)

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

    cmd = [
        sys.executable, "-m", "producer.run", slug,
        "--max-apify-spend", str(max_apify_spend),
        "--max-modal-spend", str(max_modal_spend),
    ]
    if run_mode is not None:
        cmd += ["--mode", run_mode]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from the parent process group
        )
        logger.info(
            "Spawned producer run: campaign=%s pid=%d mode=%s apify_cap=%.2f "
            "modal_cap=%.2f log=%s",
            slug, proc.pid, run_mode or "(campaign default)",
            max_apify_spend, max_modal_spend, log_path,
        )
        return {
            "started": True,
            "campaign": slug,
            "pid": proc.pid,
            "mode": run_mode,
            "max_apify_spend": max_apify_spend,
            "max_modal_spend": max_modal_spend,
        }
    except Exception as exc:
        logger.error("Failed to spawn producer for %r: %s", slug, exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": f"Failed to start producer: {exc}", "code": 500},
        )


@app.get("/api/runs/{campaign}/log", dependencies=[Depends(require_auth)])
def get_run_log(campaign: str, lines: int = 200) -> dict[str, Any]:
    """GET /api/runs/{campaign}/log — tail the producer log for a campaign.

    Makes silent producer failures legible without shell access (spec §14).
    Returns the last `lines` lines (default 200, capped at 2000).
    """
    slug = slugify(campaign)
    log_path = STORAGE_DIR / "logs" / f"producer-{slug}.log"
    if not log_path.exists():
        raise HTTPException(
            status_code=404,
            detail={"error": f"No log file for campaign {slug!r} yet", "code": 404},
        )

    lines = max(1, min(lines, 2000))
    try:
        with log_path.open("rb") as fh:
            # Read at most ~1MB from the end — plenty for a 2000-line tail.
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 1_048_576))
            tail = fh.read().decode("utf-8", errors="replace").splitlines()[-lines:]
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": f"Could not read log: {exc}", "code": 500},
        )

    return {"campaign": slug, "path": str(log_path), "lines": tail}


# ---------------------------------------------------------------------------
# /api/auth/session — cookie for <video>/<img> media requests
# ---------------------------------------------------------------------------

@app.post("/api/auth/session", dependencies=[Depends(require_auth)])
def create_auth_session() -> JSONResponse:
    """POST /api/auth/session — set the ce_session cookie.

    <video>/<img> tags cannot send an Authorization header, so clip/thumb
    media requests authenticate via this HttpOnly cookie instead. The PWA
    calls this right after unlock (and on boot with a saved token).
    """
    resp = JSONResponse({"session": True})
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=session_token(),
        max_age=30 * 24 * 3600,
        httponly=True,
        secure=True,       # Railway serves HTTPS; modern browsers also allow it on localhost
        samesite="strict",
        path="/",
    )
    return resp


@app.delete("/api/auth/session", dependencies=[Depends(require_auth)])
def destroy_auth_session() -> JSONResponse:
    """DELETE /api/auth/session — clear the session cookie (sign-out)."""
    resp = JSONResponse({"session": False})
    resp.delete_cookie(key=SESSION_COOKIE, path="/")
    return resp


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
