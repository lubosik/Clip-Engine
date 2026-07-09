"""
tests/test_config.py — campaign config loading and validation.

Tests that:
  - fitness.yaml loads and validates without errors (strict_assets=False)
  - All expected field values match the spec
  - Invalid configs fail loudly with descriptive messages
  - Asset validation raises the right errors when strict_assets=True
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml

# Locate the project root relative to this test file
PROJECT_ROOT = Path(__file__).parent.parent
FITNESS_YAML = PROJECT_ROOT / "campaigns" / "fitness.yaml"


def test_fitness_yaml_exists():
    """The demo campaign YAML must exist at the expected path."""
    assert FITNESS_YAML.exists(), f"fitness.yaml not found at {FITNESS_YAML}"


def test_fitness_yaml_loads_and_validates():
    """fitness.yaml must load and validate without errors."""
    from core.config import load_campaign

    cfg = load_campaign(FITNESS_YAML, strict_assets=False)

    assert cfg.name == "fitness"
    assert cfg.enabled is True


def test_fitness_sources():
    """fitness.yaml sources match the spec."""
    from core.config import load_campaign

    cfg = load_campaign(FITNESS_YAML, strict_assets=False)

    assert cfg.sources.youtube is not None
    # Search terms were updated to target podcast footage (real-person interviews only)
    assert "hypertrophy podcast interview" in cfg.sources.youtube.search_terms
    assert "strength training podcast huberman" in cfg.sources.youtube.search_terms
    assert cfg.sources.youtube.min_view_count == 20000
    assert cfg.sources.youtube.uploaded_within == "year"

    assert cfg.sources.tiktok is not None
    assert "fitnesspodcast" in cfg.sources.tiktok.hashtags
    assert "hypertrophy" in cfg.sources.tiktok.hashtags

    assert cfg.sources.instagram is not None


def test_fitness_ranking_defaults():
    """Ranking config must match spec §2 defaults."""
    from core.config import load_campaign

    cfg = load_campaign(FITNESS_YAML, strict_assets=False)
    r = cfg.ranking

    assert r.clip_length == [20, 60]
    assert r.max_clips_per_source == 8
    assert r.exhaust_source is False
    assert r.min_score == 0.6
    assert "EXCLUDE" in r.ranking_rules  # safety content filter present


def test_fitness_template():
    """Template config must match spec §2 values."""
    from core.config import load_campaign

    cfg = load_campaign(FITNESS_YAML, strict_assets=False)
    t = cfg.template

    assert t.aspect == "9:16"
    assert t.resolution == [1080, 1920]
    assert t.captions.style == "word_by_word"
    assert t.captions.highlight_color == "#00E5FF"
    assert t.captions.max_words_per_line == 3   # short CapCut-style chunks (updated spec)
    assert t.captions.position == "mid_low"       # caption zone below the hook box
    assert t.hook.enabled is True
    assert t.hook.show_seconds == [0, 8]
    assert t.hook.box_color == "#FFFFFF"          # white box per style refs
    assert t.hook.text_color == "#000000"         # black text per style refs
    assert t.watermark.position == "bottom"       # bottom-center, clearly readable
    assert abs(t.watermark.opacity - 0.9) < 1e-9  # high opacity (updated spec)
    assert t.corner_badge.image is None            # no corner badge per style refs
    assert t.outro.enabled is True
    assert t.outro.audio == "keep"


def test_fitness_destinations():
    """Destinations config must match spec §2 values."""
    from core.config import load_campaign

    cfg = load_campaign(FITNESS_YAML, strict_assets=False)
    d = cfg.destinations

    assert "instagram-standalone" in d.postiz_channels
    assert "x" in d.postiz_channels
    assert d.schedule.posts_per_day == 1
    assert "17:00" in d.schedule.times
    assert d.autopost is False
    assert "#fitness" in d.hashtags
    assert "#hypertrophy" in d.hashtags


def test_fitness_analytics():
    from core.config import load_campaign

    cfg = load_campaign(FITNESS_YAML, strict_assets=False)
    assert cfg.analytics.track is True
    assert cfg.analytics.pull_day == "monday"


def test_fitness_asset_paths_listed():
    """Asset paths should be non-empty strings pointing to assets/fitness/."""
    from core.config import load_campaign

    cfg = load_campaign(FITNESS_YAML, strict_assets=False)
    paths = cfg.asset_paths()

    assert all(isinstance(p, str) and p for p in paths)
    assert all("fitness" in p for p in paths)


def test_strict_assets_fails_loudly_when_missing(tmp_path):
    """strict_assets=True must raise ValueError listing ALL missing files."""
    from core.config import load_campaign

    # Write a minimal valid YAML with non-existent asset paths
    yaml_content = textwrap.dedent("""
        name: test_campaign
        enabled: true
        sources:
          youtube:
            search_terms: ["test"]
            min_view_count: 1000
            uploaded_within: "week"
        ranking:
          clip_length: [20, 60]
          max_clips_per_source: 8
          exhaust_source: false
          min_score: 0.6
          ranking_rules: "Test rules"
        template:
          aspect: "9:16"
          resolution: [1080, 1920]
          captions:
            style: "word_by_word"
            font: "assets/test_campaign/DoesNotExist.ttf"
            base_color: "#FFFFFF"
            highlight_color: "#00E5FF"
            outline_color: "#000000"
            outline_px: 6
            position: "upper_mid"
            max_words_per_line: 4
          hook:
            enabled: true
            show_seconds: [0, 8]
            source: "ranking"
            font: "assets/test_campaign/DoesNotExist.ttf"
            box_color: "#111111CC"
          lower_third:
            show_source_handle: true
            format: "via @{source_handle}"
          watermark:
            image: "assets/test_campaign/logo.png"
            position: "center"
            opacity: 0.18
            scale: 0.5
          corner_badge:
            image: "assets/test_campaign/logo_circle.png"
            position: "top_right"
            opacity: 1.0
            scale: 0.12
          outro:
            enabled: true
            clip: "assets/test_campaign/outro.mov"
            audio: "keep"
        destinations:
          postiz_channels: ["tiktok_test"]
          schedule:
            posts_per_day: 1
            times: ["17:00"]
            timezone: "America/New_York"
          caption_template: "{hook}\\n\\nvia @{source_handle}\\n{hashtags}"
          hashtags: ["#test"]
          autopost: false
        analytics:
          track: true
          pull_day: "monday"
    """)

    campaigns_dir = tmp_path / "campaigns"
    campaigns_dir.mkdir()
    yaml_file = campaigns_dir / "test_campaign.yaml"
    yaml_file.write_text(yaml_content)

    with pytest.raises(ValueError) as exc_info:
        load_campaign(yaml_file, strict_assets=True)

    error_msg = str(exc_info.value)
    # Should name at least one missing file
    assert "DoesNotExist.ttf" in error_msg or "logo.png" in error_msg or "outro.mov" in error_msg


def test_invalid_name_rejected(tmp_path):
    """Campaign names with spaces must be rejected."""
    from core.config import load_campaign

    bad_yaml = textwrap.dedent("""
        name: "bad name"
        enabled: true
        sources:
          youtube:
            search_terms: ["test"]
        ranking:
          clip_length: [20, 60]
          ranking_rules: "test"
        template:
          captions:
            style: word_by_word
            font: "some.ttf"
          hook:
            font: "some.ttf"
          lower_third: {}
          watermark:
            image: "logo.png"
          corner_badge:
            image: "badge.png"
          outro:
            clip: "outro.mov"
        destinations:
          postiz_channels: ["ch1"]
          schedule: {}
          caption_template: "{hook}"
          hashtags: []
        analytics: {}
    """)
    f = tmp_path / "bad.yaml"
    f.write_text(bad_yaml)

    with pytest.raises(ValueError):
        load_campaign(f, strict_assets=False)


def test_invalid_clip_length_rejected(tmp_path):
    """clip_length where max <= min must be rejected."""
    from core.config import RankingConfig
    import pydantic

    with pytest.raises((ValueError, pydantic.ValidationError)):
        RankingConfig(
            clip_length=[60, 20],  # max < min — invalid
            max_clips_per_source=8,
            min_score=0.6,
            ranking_rules="test",
        )


def test_invalid_min_score_rejected():
    """min_score outside [0, 1] must be rejected."""
    from core.config import RankingConfig
    import pydantic

    with pytest.raises((ValueError, pydantic.ValidationError)):
        RankingConfig(
            clip_length=[20, 60],
            max_clips_per_source=8,
            min_score=1.5,  # > 1
            ranking_rules="test",
        )


def test_invalid_uploaded_within_rejected():
    """uploaded_within with unknown value must be rejected."""
    from core.config import YouTubeSourceConfig
    import pydantic

    with pytest.raises((ValueError, pydantic.ValidationError)):
        YouTubeSourceConfig(uploaded_within="fortnight")


def test_invalid_outro_audio_rejected():
    """outro.audio must be 'keep' or 'mute'."""
    from core.config import OutroConfig
    import pydantic

    with pytest.raises((ValueError, pydantic.ValidationError)):
        OutroConfig(clip="outro.mov", audio="silent")


def test_invalid_analytics_pull_day_rejected():
    """pull_day must be a valid weekday name."""
    from core.config import AnalyticsConfig
    import pydantic

    with pytest.raises((ValueError, pydantic.ValidationError)):
        AnalyticsConfig(pull_day="everyday")


def test_no_sources_rejected():
    """A campaign with no source platforms at all must fail validation."""
    from core.config import SourcesConfig
    import pydantic

    with pytest.raises((ValueError, pydantic.ValidationError)):
        SourcesConfig()  # all None — should fail


def test_load_enabled_campaigns(tmp_path):
    """load_enabled_campaigns returns only enabled campaigns."""
    from core.config import load_enabled_campaigns

    # Create a valid enabled campaign
    enabled_yaml = textwrap.dedent("""
        name: enabled_test
        enabled: true
        sources:
          youtube:
            search_terms: ["test"]
        ranking:
          clip_length: [20, 60]
          ranking_rules: "test"
        template:
          captions:
            style: word_by_word
            font: "f.ttf"
          hook:
            font: "f.ttf"
          lower_third: {}
          watermark:
            image: "logo.png"
          corner_badge:
            image: "badge.png"
          outro:
            clip: "outro.mov"
        destinations:
          postiz_channels: ["ch1"]
          schedule: {}
          caption_template: "{hook}"
          hashtags: []
        analytics: {}
    """)

    disabled_yaml = textwrap.dedent("""
        name: disabled_test
        enabled: false
        sources:
          youtube:
            search_terms: ["test"]
        ranking:
          clip_length: [20, 60]
          ranking_rules: "test"
        template:
          captions:
            style: word_by_word
            font: "f.ttf"
          hook:
            font: "f.ttf"
          lower_third: {}
          watermark:
            image: "logo.png"
          corner_badge:
            image: "badge.png"
          outro:
            clip: "outro.mov"
        destinations:
          postiz_channels: ["ch1"]
          schedule: {}
          caption_template: "{hook}"
          hashtags: []
        analytics: {}
    """)

    (tmp_path / "enabled_test.yaml").write_text(enabled_yaml)
    (tmp_path / "disabled_test.yaml").write_text(disabled_yaml)

    results = load_enabled_campaigns(str(tmp_path), strict_assets=False)
    names = [c.name for c in results]

    assert "enabled_test" in names
    assert "disabled_test" not in names
