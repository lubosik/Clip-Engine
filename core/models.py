"""
core/models.py — SQLAlchemy ORM models (canonical).

All other agents import from here. Do not duplicate table definitions
elsewhere. Every table has created_at and updated_at UTC timestamps.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on Postgres (production), plain JSON elsewhere (SQLite tests)
JSONB = _PG_JSONB().with_variant(JSON(), "sqlite")


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# campaigns
# ---------------------------------------------------------------------------

class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Snapshot of the loaded config dict at last run
    config_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    sources: Mapped[list[Source]] = relationship("Source", back_populates="campaign_rel")
    clips: Mapped[list[Clip]] = relationship("Clip", back_populates="campaign_rel")


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # "{platform}:{native_id}" — globally unique across all campaigns
    source_id: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    campaign: Mapped[str] = mapped_column(
        String(128), ForeignKey("campaigns.name", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)  # youtube|tiktok|instagram
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_handle: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Raw metadata from the discovery actor ("metadata" is reserved on
    # DeclarativeBase, so the attribute is source_metadata; column name stays)
    source_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    # pending | selected | done | partially_done
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    # [[start, end], ...] float seconds — ranges already cut into clips
    used_ranges: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    campaign_rel: Mapped[Campaign] = relationship("Campaign", back_populates="sources")
    transcript: Mapped[Transcript | None] = relationship(
        "Transcript", back_populates="source_rel", uselist=False
    )
    clips: Mapped[list[Clip]] = relationship("Clip", back_populates="source_rel")
    comments: Mapped[list[Comment]] = relationship("Comment", back_populates="source_rel")

    __table_args__ = (
        Index("ix_sources_source_id", "source_id"),
        Index("ix_sources_campaign_status", "campaign", "status"),
    )


# ---------------------------------------------------------------------------
# transcripts
# ---------------------------------------------------------------------------

class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(
        String(512), ForeignKey("sources.source_id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # [{start: float, end: float, text: str}]
    segments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # True when word-level timestamps are available
    word_level: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    source_rel: Mapped[Source] = relationship("Source", back_populates="transcript")


# ---------------------------------------------------------------------------
# clips
# ---------------------------------------------------------------------------

class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign: Mapped[str] = mapped_column(
        String(128), ForeignKey("campaigns.name", ondelete="CASCADE"), nullable=False
    )
    # Nullable — memes have no source video (migration 002)
    source_id: Mapped[str | None] = mapped_column(
        String(512), ForeignKey("sources.source_id", ondelete="CASCADE"), nullable=True
    )
    start: Mapped[float | None] = mapped_column(Float, nullable=True)
    end: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 'clip' | 'meme' — identifies content type (migration 002)
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="clip")
    # 'demo' | 'production' — stamped at creation from campaign mode (migration 002)
    mode: Mapped[str] = mapped_column(String(12), nullable=False, default="production")
    # '9:16' | '1:1' | '4:5' — aspect ratio of rendered output (migration 002)
    aspect: Mapped[str] = mapped_column(String(8), nullable=False, default="9:16")
    # For memes: {concept, classifier_scores, profile_version} (migration 002)
    meme_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # AI review gate (migration 003)
    # pending | ready | didnt_pass | overridden
    gate_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # [{phase, check, pass, reason}] — populated by producer/review_gate.run_gate()
    gate_reasons: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # 0.0-1.0 average of the §6c 10-question rubric; NULL until Phase 2 runs
    formula_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    hook: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumb_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ["tiktok_fitness", "instagram_fitness", ...]
    destination_channels: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # pending_review | approved | rejected | scheduled | posted
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    # {"tiktok_fitness": "postiz_post_id", ...}
    postiz_post_ids: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # {"tiktok_fitness": "https://tiktok.com/...", ...}
    posted_permalinks: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    campaign_rel: Mapped[Campaign] = relationship("Campaign", back_populates="clips")
    # Optional because meme clips have no source (source_id is NULL)
    source_rel: Mapped[Source | None] = relationship("Source", back_populates="clips")
    analytics: Mapped[list[Analytics]] = relationship("Analytics", back_populates="clip_rel")

    __table_args__ = (
        Index("ix_clips_status", "status"),
        Index("ix_clips_campaign", "campaign"),
        Index("ix_clips_kind", "kind"),
        Index("ix_clips_gate_status", "gate_status"),
    )


# ---------------------------------------------------------------------------
# comments
# ---------------------------------------------------------------------------

class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(
        String(512), ForeignKey("sources.source_id", ondelete="CASCADE"), nullable=False
    )
    # The URL of the post this comment belongs to (for per-post attribution)
    post_url: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    source_rel: Mapped[Source] = relationship("Source", back_populates="comments")

    __table_args__ = (
        Index("ix_comments_source_id", "source_id"),
        Index("ix_comments_post_url", "post_url"),
    )


# ---------------------------------------------------------------------------
# analytics
# ---------------------------------------------------------------------------

class Analytics(Base):
    __tablename__ = "analytics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clip_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)  # tiktok|instagram|x
    pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    views: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    likes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    shares: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    clip_rel: Mapped[Clip] = relationship("Clip", back_populates="analytics")

    __table_args__ = (
        Index("ix_analytics_clip_id", "clip_id"),
        Index("ix_analytics_clip_platform", "clip_id", "platform"),
    )


# ---------------------------------------------------------------------------
# render_jobs — Modal spend ledger (migration 002)
# ---------------------------------------------------------------------------

class RenderJob(Base):
    """One row per render invocation (Modal or local ffmpeg fallback).

    Powers the /api/spend endpoint and the --max-modal-spend producer guard.
    Costs are estimates based on recorded wall-clock duration × published rate.
    """

    __tablename__ = "render_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL when clip was deleted or for test/healthcheck jobs
    clip_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("clips.id", ondelete="SET NULL"), nullable=True
    )
    campaign: Mapped[str] = mapped_column(String(128), nullable=False)
    # 'modal' | 'local'
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    # GPU type returned by Modal ('l4', 't4', 'a10g', 'any', …); NULL for local
    gpu: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_s: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rate_per_s: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_estimate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 'ok' | 'error'
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_render_jobs_campaign", "campaign"),
        Index("ix_render_jobs_created_at", "created_at"),
    )


# ---------------------------------------------------------------------------
# meme_profiles — versioned meme style profiles (migration 002)
# ---------------------------------------------------------------------------

class MemeProfile(Base):
    """Extracted meme style profile for a campaign.

    New versions are created by the weekly feedback loop (meme/feedback.py).
    The active profile is the row with the highest version for a given campaign.
    """

    __tablename__ = "meme_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Extracted style profile dict: {visual_format, caption_voice, rules, confidence}
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("campaign", "version", name="uq_meme_profiles_campaign_version"),
        Index("ix_meme_profiles_campaign", "campaign"),
    )
