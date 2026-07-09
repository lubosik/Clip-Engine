"""
core/config.py — campaign YAML loader and pydantic v2 schema validation.

Public interface (per ARCHITECTURE §4):
    load_campaign(path: str | Path) -> CampaignConfig
    load_enabled_campaigns(dir: str = "campaigns") -> list[CampaignConfig]

Asset paths are validated to exist when strict_assets=True (producer runs).
Config-only or test runs can pass strict_assets=False (the default).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sub-models — sources
# ---------------------------------------------------------------------------

class YouTubeSourceConfig(BaseModel):
    search_terms: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    min_view_count: int = 0
    uploaded_within: str = "year"  # hour|day|week|month|year
    # Optional blocklist: channel name substrings to skip (case-insensitive).
    # Matched against the candidate's author_handle / channelName field.
    # Example: ["BadChannel", "spammer123"]
    # Note: do NOT edit campaigns/fitness.yaml; this key is supported in new campaigns.
    exclude_channels: list[str] = Field(default_factory=list)

    @field_validator("uploaded_within")
    @classmethod
    def _valid_uploaded_within(cls, v: str) -> str:
        allowed = {"hour", "day", "week", "month", "year"}
        if v not in allowed:
            raise ValueError(f"uploaded_within must be one of {allowed}, got {v!r}")
        return v


class TikTokSourceConfig(BaseModel):
    profiles: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)


class InstagramSourceConfig(BaseModel):
    profiles: list[str] = Field(default_factory=list)


class SourcesConfig(BaseModel):
    youtube: YouTubeSourceConfig | None = None
    tiktok: TikTokSourceConfig | None = None
    instagram: InstagramSourceConfig | None = None
    # Cross-platform title/caption keyword blocklist (case-insensitive substring match).
    # Applied during discover_all() to filter candidates from any platform.
    # Example: ["sponsored", "ad", "promotion"]
    exclude_keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _at_least_one_source(self) -> "SourcesConfig":
        if self.youtube is None and self.tiktok is None and self.instagram is None:
            raise ValueError(
                "At least one source platform (youtube, tiktok, instagram) must be configured."
            )
        return self


# ---------------------------------------------------------------------------
# Sub-models — ranking
# ---------------------------------------------------------------------------

class RankingConfig(BaseModel):
    clip_length: list[int] = Field(default=[20, 60])
    max_clips_per_source: int = 8
    exhaust_source: bool = False
    min_score: float = 0.6
    ranking_rules: str = (
        "Prefer moments that are genuinely useful or interesting on their own. "
        "Each clip must stand alone. EXCLUDE: unsafe advice, harmful content, "
        "anything that violates platform community guidelines."
    )

    @field_validator("clip_length")
    @classmethod
    def _valid_clip_length(cls, v: list[int]) -> list[int]:
        if len(v) != 2:
            raise ValueError("clip_length must be [min_seconds, max_seconds]")
        if v[0] <= 0:
            raise ValueError(f"clip_length min must be > 0, got {v[0]}")
        if v[1] <= v[0]:
            raise ValueError(
                f"clip_length max ({v[1]}) must be greater than min ({v[0]})"
            )
        return v

    @field_validator("min_score")
    @classmethod
    def _valid_min_score(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"min_score must be between 0.0 and 1.0, got {v}")
        return v

    @field_validator("max_clips_per_source")
    @classmethod
    def _valid_max_clips(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_clips_per_source must be >= 1, got {v}")
        return v


# ---------------------------------------------------------------------------
# Sub-models — template
# ---------------------------------------------------------------------------

class CaptionsConfig(BaseModel):
    style: str = "word_by_word"
    # None → renderer falls back to a system font (DejaVu Sans Bold)
    font: str | None = None
    base_color: str = "#FFFFFF"
    highlight_color: str = "#00E5FF"
    outline_color: str = "#000000"
    outline_px: int = 6
    position: str = "upper_mid"
    max_words_per_line: int = 4


class HookConfig(BaseModel):
    enabled: bool = True
    show_seconds: list[int] = Field(default=[0, 8])
    source: str = "ranking"
    font: str | None = None
    box_color: str = "#111111CC"
    # Text color drawn inside the box (style refs: black text on a white box)
    text_color: str = "#FFFFFF"


class LowerThirdConfig(BaseModel):
    show_source_handle: bool = True
    format: str = "via @{source_handle}"


class WatermarkConfig(BaseModel):
    # None → watermark skipped at render time
    image: str | None = None
    position: str = "center"
    opacity: float = 0.18
    scale: float = 0.5


class CornerBadgeConfig(BaseModel):
    # None → badge skipped at render time
    image: str | None = None
    position: str = "top_right"
    opacity: float = 1.0
    scale: float = 0.12


class OutroConfig(BaseModel):
    enabled: bool = True
    # Required only when enabled
    clip: str | None = None
    audio: str = "keep"  # keep|mute

    @field_validator("audio")
    @classmethod
    def _valid_audio(cls, v: str) -> str:
        if v not in {"keep", "mute"}:
            raise ValueError(f"outro.audio must be 'keep' or 'mute', got {v!r}")
        return v

    @model_validator(mode="after")
    def _clip_required_when_enabled(self) -> "OutroConfig":
        if self.enabled and not self.clip:
            raise ValueError("outro.clip is required when outro.enabled is true")
        return self


class TemplateConfig(BaseModel):
    aspect: str = "9:16"
    resolution: list[int] = Field(default=[1080, 1920])
    captions: CaptionsConfig
    hook: HookConfig
    lower_third: LowerThirdConfig
    watermark: WatermarkConfig
    corner_badge: CornerBadgeConfig
    outro: OutroConfig

    @field_validator("resolution")
    @classmethod
    def _valid_resolution(cls, v: list[int]) -> list[int]:
        if len(v) != 2 or any(x <= 0 for x in v):
            raise ValueError(f"resolution must be [width, height] with positive integers, got {v}")
        return v


# ---------------------------------------------------------------------------
# Sub-models — destinations
# ---------------------------------------------------------------------------

class ScheduleConfig(BaseModel):
    posts_per_day: int = 1
    times: list[str] = Field(default=["17:00"])
    timezone: str = "America/New_York"


class DestinationsConfig(BaseModel):
    postiz_channels: list[str]
    schedule: ScheduleConfig
    caption_template: str
    hashtags: list[str] = Field(default_factory=list)
    autopost: bool = False


# ---------------------------------------------------------------------------
# Sub-models — analytics
# ---------------------------------------------------------------------------

class AnalyticsConfig(BaseModel):
    track: bool = True
    pull_day: str = "monday"

    @field_validator("pull_day")
    @classmethod
    def _valid_pull_day(cls, v: str) -> str:
        allowed = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
        if v.lower() not in allowed:
            raise ValueError(f"analytics.pull_day must be a day name, got {v!r}")
        return v.lower()


# ---------------------------------------------------------------------------
# Sub-models — revamp v2 additions (all optional with defaults)
# ---------------------------------------------------------------------------

class EnginesConfig(BaseModel):
    """Controls which content engines are active for a campaign."""

    clips: bool = True
    memes: bool = False


class MemeConfig(BaseModel):
    """Meme engine configuration (required only when engines.memes is True).

    All fields have safe defaults so the model can be instantiated without
    raising even when memes are disabled — this avoids brittle conditional logic
    in the loader.
    """

    # Local directory holding reference meme images; relative to project root
    refs_dir: str = ""
    # Override for the image generation model (falls back to MEME_IMAGE_MODEL env)
    image_model: str = ""
    # Campaign-specific hard rules merged with the global defaults:
    #   - no em-dashes
    #   - no medical/health claims
    #   - no unsafe dieting / disordered eating content
    hard_rules: list[str] = Field(default_factory=list)


class DemoConfig(BaseModel):
    """Demo-mode settings (optional; demo items can still post to real channels)."""

    # Postiz channel IDs to use when posting demo items instead of live channels
    test_channels: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level campaign config
# ---------------------------------------------------------------------------

class CampaignConfig(BaseModel):
    name: str
    enabled: bool = True
    # demo | production — default mode for runs and clips created by this campaign
    mode: str = "demo"
    engines: EnginesConfig = Field(default_factory=EnginesConfig)
    # Free-text brief fed into ranking + render guidance; optional
    creative_direction: str = ""
    # Meme engine config — only required when engines.memes is True
    meme: MemeConfig | None = None
    # Demo-mode overrides — optional
    demo: DemoConfig | None = None
    sources: SourcesConfig
    ranking: RankingConfig
    template: TemplateConfig
    destinations: DestinationsConfig
    analytics: AnalyticsConfig

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v not in {"demo", "production"}:
            raise ValueError(f"mode must be 'demo' or 'production', got {v!r}")
        return v

    # Populated by load_campaign — not in the YAML
    _yaml_path: Path | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Campaign name must not be empty.")
        # Ensure filesystem-safe name
        safe = v.strip().replace(" ", "_")
        if safe != v:
            raise ValueError(
                f"Campaign name must not contain spaces or be blank; "
                f"use underscores instead (got {v!r})."
            )
        return v

    def asset_paths(self) -> list[str]:
        """Return all asset file paths referenced by this config (set fields only)."""
        paths = [
            self.template.captions.font,
            self.template.hook.font,
            self.template.watermark.image,
            self.template.corner_badge.image,
        ]
        if self.template.outro.enabled:
            paths.append(self.template.outro.clip)
        return [p for p in paths if p]

    def validate_assets(self, base_dir: Path | None = None) -> None:
        """
        Raise ValueError listing all missing asset files.

        base_dir: if provided, resolves relative paths against it.
                  Defaults to the process cwd.
        """
        missing = []
        for p in self.asset_paths():
            resolved = Path(p) if Path(p).is_absolute() else (base_dir or Path.cwd()) / p
            if not resolved.exists():
                missing.append(str(resolved))
        if missing:
            raise ValueError(
                f"Campaign '{self.name}' is missing required asset files:\n"
                + "\n".join(f"  - {m}" for m in missing)
                + "\nPlace the files at the listed paths or update the YAML."
            )


# ---------------------------------------------------------------------------
# Public loader functions
# ---------------------------------------------------------------------------

def load_campaign_dict(config: dict[str, Any]) -> CampaignConfig:
    """Validate an in-memory campaign config dict (used by the wizard API).

    Raises ValueError with a clear message if validation fails.
    """
    try:
        return CampaignConfig.model_validate(config)
    except Exception as exc:
        raise ValueError(f"Campaign config failed validation:\n{exc}") from exc


def load_campaign(
    path: str | Path,
    *,
    strict_assets: bool = False,
) -> CampaignConfig:
    """
    Load and validate a campaign YAML file.

    strict_assets=True  — validates that every referenced asset file exists;
                          use this in producer runs.
    strict_assets=False — skips asset existence checks; safe for tests and
                          config-only validation.

    Raises:
        FileNotFoundError  if the YAML file does not exist.
        ValueError         with a clear message if validation fails.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Campaign file not found: {path}")

    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Campaign file must be a YAML mapping, got {type(raw).__name__}: {path}")

    try:
        cfg = CampaignConfig.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"Campaign '{path}' failed validation:\n{exc}") from exc

    cfg._yaml_path = path

    if strict_assets:
        cfg.validate_assets(base_dir=path.parent.parent)  # campaigns/ is one level under root

    log.info(
        "Loaded campaign",
        extra={"campaign": cfg.name, "path": str(path), "enabled": cfg.enabled},
    )
    return cfg


def load_enabled_campaigns(
    dir: str = "campaigns",
    *,
    strict_assets: bool = False,
) -> list[CampaignConfig]:
    """
    Load all enabled campaign YAMLs from a directory.
    Skips files that fail validation with a warning (does not crash the run).
    """
    campaigns_dir = Path(dir)
    if not campaigns_dir.exists():
        raise FileNotFoundError(f"Campaigns directory not found: {campaigns_dir.resolve()}")

    results: list[CampaignConfig] = []
    for yaml_file in sorted(campaigns_dir.glob("*.yaml")):
        try:
            cfg = load_campaign(yaml_file, strict_assets=strict_assets)
            if cfg.enabled:
                results.append(cfg)
            else:
                log.info("Skipping disabled campaign", extra={"campaign": cfg.name})
        except (ValueError, FileNotFoundError) as exc:
            log.warning(
                "Skipping invalid campaign file",
                extra={"file": str(yaml_file), "error": str(exc)},
            )

    if not results:
        log.warning("No enabled campaigns found", extra={"dir": str(campaigns_dir)})

    return results
