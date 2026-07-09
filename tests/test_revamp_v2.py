"""
tests/test_revamp_v2.py — Focused unit tests for revamp v2 contracts.

Covers:
  - Migration-model parity: Clip, RenderJob, MemeProfile have the correct fields
  - Config new-fields defaults and fitness.yaml still validates
  - modal_costs: GPU rates and estimate functions
  - Spend endpoint aggregation (SQLite in-memory)
  - Schedule label formatting
  - Storage R2 key helpers
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
FITNESS_YAML = PROJECT_ROOT / "campaigns" / "fitness.yaml"


# ---------------------------------------------------------------------------
# 1. Migration-model parity — Clip, RenderJob, MemeProfile columns
# ---------------------------------------------------------------------------

class TestModelParity:
    """Clip, RenderJob, MemeProfile must have all columns defined in migration 002."""

    def test_clip_has_kind(self):
        from core.models import Clip
        assert hasattr(Clip, "kind"), "Clip must have 'kind' column (migration 002)"

    def test_clip_has_mode(self):
        from core.models import Clip
        assert hasattr(Clip, "mode"), "Clip must have 'mode' column (migration 002)"

    def test_clip_has_aspect(self):
        from core.models import Clip
        assert hasattr(Clip, "aspect"), "Clip must have 'aspect' column (migration 002)"

    def test_clip_has_meme_meta(self):
        from core.models import Clip
        assert hasattr(Clip, "meme_meta"), "Clip must have 'meme_meta' column (migration 002)"

    def test_clip_source_id_nullable(self):
        """source_id must be nullable so meme clips can omit it."""
        from core.models import Clip
        col = Clip.__table__.c.source_id
        assert col.nullable, "Clip.source_id must be nullable (meme clips have no source)"

    def test_clip_start_nullable(self):
        from core.models import Clip
        col = Clip.__table__.c.start
        assert col.nullable, "Clip.start must be nullable (meme clips have no time range)"

    def test_clip_end_nullable(self):
        from core.models import Clip
        col = Clip.__table__.c.end
        assert col.nullable, "Clip.end must be nullable (meme clips have no time range)"

    def test_render_job_model_exists(self):
        from core.models import RenderJob
        assert RenderJob is not None

    def test_render_job_has_required_columns(self):
        from core.models import RenderJob
        table = RenderJob.__table__
        col_names = {c.name for c in table.c}
        required = {
            "id", "clip_id", "campaign", "backend", "gpu",
            "duration_s", "rate_per_s", "cost_estimate",
            "status", "error", "created_at", "updated_at",
        }
        missing = required - col_names
        assert not missing, f"RenderJob missing columns: {missing}"

    def test_render_job_clip_id_nullable(self):
        from core.models import RenderJob
        col = RenderJob.__table__.c.clip_id
        assert col.nullable, "RenderJob.clip_id must be nullable"

    def test_meme_profile_model_exists(self):
        from core.models import MemeProfile
        assert MemeProfile is not None

    def test_meme_profile_has_required_columns(self):
        from core.models import MemeProfile
        table = MemeProfile.__table__
        col_names = {c.name for c in table.c}
        required = {"id", "campaign", "version", "profile", "created_at", "updated_at"}
        missing = required - col_names
        assert not missing, f"MemeProfile missing columns: {missing}"

    def test_meme_profile_unique_constraint(self):
        """campaign + version must be unique."""
        from core.models import MemeProfile
        constraint_names = {
            c.name
            for c in MemeProfile.__table__.constraints
        }
        assert "uq_meme_profiles_campaign_version" in constraint_names


# ---------------------------------------------------------------------------
# 2. Config new-fields defaults + fitness.yaml validates
# ---------------------------------------------------------------------------

class TestConfigNewFields:
    """New optional fields in CampaignConfig must have correct defaults."""

    def test_engines_config_defaults(self):
        from core.config import EnginesConfig
        e = EnginesConfig()
        assert e.clips is True
        assert e.memes is False

    def test_meme_config_defaults(self):
        from core.config import MemeConfig
        m = MemeConfig()
        assert m.refs_dir == ""
        assert m.image_model == ""
        assert m.hard_rules == []

    def test_demo_config_defaults(self):
        from core.config import DemoConfig
        d = DemoConfig()
        assert d.test_channels == []

    def test_campaign_config_mode_default(self):
        """CampaignConfig.mode defaults to 'demo'."""
        from core.config import load_campaign
        cfg = load_campaign(FITNESS_YAML, strict_assets=False)
        # fitness.yaml doesn't set mode → default is 'demo'
        assert cfg.mode == "demo"

    def test_campaign_config_engines_default(self):
        """CampaignConfig.engines defaults to {clips: True, memes: False}."""
        from core.config import load_campaign
        cfg = load_campaign(FITNESS_YAML, strict_assets=False)
        assert cfg.engines.clips is True
        assert cfg.engines.memes is False

    def test_campaign_config_creative_direction_default(self):
        from core.config import load_campaign
        cfg = load_campaign(FITNESS_YAML, strict_assets=False)
        assert cfg.creative_direction == ""

    def test_campaign_config_meme_default_none(self):
        from core.config import load_campaign
        cfg = load_campaign(FITNESS_YAML, strict_assets=False)
        assert cfg.meme is None

    def test_campaign_config_demo_default_none(self):
        from core.config import load_campaign
        cfg = load_campaign(FITNESS_YAML, strict_assets=False)
        assert cfg.demo is None

    def test_fitness_yaml_still_validates(self):
        """fitness.yaml must still load cleanly after the new field additions."""
        from core.config import load_campaign
        cfg = load_campaign(FITNESS_YAML, strict_assets=False)
        assert cfg.name == "fitness"
        assert cfg.enabled is True

    def test_invalid_mode_rejected(self):
        from core.config import CampaignConfig
        import pydantic
        with pytest.raises((ValueError, pydantic.ValidationError)):
            CampaignConfig.model_validate({"name": "x", "mode": "staging"})

    def test_campaign_with_explicit_mode_production(self, tmp_path):
        import textwrap
        from core.config import load_campaign
        yaml_content = textwrap.dedent("""
            name: prod_test
            enabled: true
            mode: production
            engines:
              clips: true
              memes: true
            sources:
              youtube:
                search_terms: ["test"]
            ranking:
              clip_length: [20, 60]
              ranking_rules: "test"
            template:
              captions:
                style: word_by_word
              hook: {}
              lower_third: {}
              watermark: {}
              corner_badge: {}
              outro:
                enabled: false
            destinations:
              postiz_channels: ["ch1"]
              schedule: {}
              caption_template: "{hook}"
              hashtags: []
            analytics: {}
        """)
        (tmp_path / "prod_test.yaml").write_text(yaml_content)
        cfg = load_campaign(tmp_path / "prod_test.yaml", strict_assets=False)
        assert cfg.mode == "production"
        assert cfg.engines.clips is True
        assert cfg.engines.memes is True


# ---------------------------------------------------------------------------
# 3. modal_costs — GPU rates and estimate functions
# ---------------------------------------------------------------------------

class TestModalCosts:
    """GPU rate table and estimate functions."""

    def test_known_gpu_rates(self):
        from core.modal_costs import GPU_RATES
        assert GPU_RATES["l4"] == pytest.approx(0.000222)
        assert GPU_RATES["t4"] == pytest.approx(0.000164)
        assert GPU_RATES["a10g"] == pytest.approx(0.000306)
        assert GPU_RATES["any"] == pytest.approx(0.000306)

    def test_rate_for_known_gpus(self):
        from core.modal_costs import rate_for
        assert rate_for("l4") == pytest.approx(0.000222)
        assert rate_for("t4") == pytest.approx(0.000164)
        assert rate_for("a10g") == pytest.approx(0.000306)
        assert rate_for("any") == pytest.approx(0.000306)

    def test_rate_for_unknown_gpu_falls_back_to_any(self):
        from core.modal_costs import rate_for, GPU_RATES
        assert rate_for("a100") == pytest.approx(GPU_RATES["any"])
        assert rate_for("unknown_future_gpu") == pytest.approx(GPU_RATES["any"])

    def test_rate_for_none_falls_back_to_any(self):
        from core.modal_costs import rate_for, GPU_RATES
        assert rate_for(None) == pytest.approx(GPU_RATES["any"])

    def test_rate_for_case_insensitive(self):
        from core.modal_costs import rate_for
        assert rate_for("L4") == pytest.approx(0.000222)
        assert rate_for("T4") == pytest.approx(0.000164)

    def test_estimate_cost_l4_10s(self):
        from core.modal_costs import estimate_cost
        cost = estimate_cost("l4", 10.0)
        assert cost == pytest.approx(0.000222 * 10.0)

    def test_estimate_cost_t4_60s(self):
        from core.modal_costs import estimate_cost
        cost = estimate_cost("t4", 60.0)
        assert cost == pytest.approx(0.000164 * 60.0)

    def test_estimate_cost_zero_duration(self):
        from core.modal_costs import estimate_cost
        assert estimate_cost("l4", 0.0) == pytest.approx(0.0)

    def test_estimate_cost_negative_duration_clamped(self):
        from core.modal_costs import estimate_cost
        assert estimate_cost("l4", -5.0) == pytest.approx(0.0)

    def test_estimate_cost_none_gpu_uses_fallback(self):
        from core.modal_costs import estimate_cost, GPU_RATES
        cost = estimate_cost(None, 100.0)
        assert cost == pytest.approx(GPU_RATES["any"] * 100.0)


# ---------------------------------------------------------------------------
# 4. Spend endpoint aggregation — SQLite in-memory
# ---------------------------------------------------------------------------

class TestSpendAggregation:
    """_compute_spend_data aggregates render_jobs correctly."""

    @pytest.fixture
    def sqlite_session(self, monkeypatch):
        """Create an in-memory SQLite session with the full schema."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from core.models import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        session = SessionLocal()
        # Patch settings so _compute_spend_data can read MODAL_MONTHLY_BUDGET
        monkeypatch.setenv("MODAL_MONTHLY_BUDGET", "30.0")

        # Invalidate the lru_cache so it picks up the new env var
        from core.settings import get_settings
        get_settings.cache_clear()

        yield session
        session.close()
        engine.dispose()

        # Restore cache
        get_settings.cache_clear()

    def test_empty_returns_zero_mtd(self, sqlite_session):
        from web.api import _compute_spend_data
        result = _compute_spend_data(sqlite_session, months=1)
        assert result["estimated"] is True
        assert result["month_to_date_usd"] == pytest.approx(0.0)
        assert result["remaining_credit_usd"] == pytest.approx(30.0)
        assert result["by_campaign"] == []
        assert result["recent"] == []
        assert "plan_note" in result

    def test_jobs_aggregate_correctly(self, sqlite_session):
        from core.models import RenderJob
        from web.api import _compute_spend_data

        now = datetime.now(timezone.utc)
        jobs = [
            RenderJob(
                campaign="fitness",
                backend="modal",
                gpu="l4",
                duration_s=10.0,
                rate_per_s=0.000222,
                cost_estimate=0.00222,
                status="ok",
                created_at=now,
                updated_at=now,
            ),
            RenderJob(
                campaign="fitness",
                backend="modal",
                gpu="t4",
                duration_s=20.0,
                rate_per_s=0.000164,
                cost_estimate=0.00328,
                status="ok",
                created_at=now,
                updated_at=now,
            ),
            RenderJob(
                campaign="other",
                backend="modal",
                gpu="l4",
                duration_s=5.0,
                rate_per_s=0.000222,
                cost_estimate=0.00111,
                status="ok",
                created_at=now,
                updated_at=now,
            ),
        ]
        sqlite_session.add_all(jobs)
        sqlite_session.commit()

        result = _compute_spend_data(sqlite_session, months=1)
        expected_mtd = 0.00222 + 0.00328 + 0.00111
        assert result["month_to_date_usd"] == pytest.approx(expected_mtd, rel=1e-5)
        assert result["remaining_credit_usd"] == pytest.approx(30.0 - expected_mtd, rel=1e-5)

        # by_campaign should have fitness first (higher spend)
        campaigns = {r["campaign"]: r for r in result["by_campaign"]}
        assert "fitness" in campaigns
        assert "other" in campaigns
        assert campaigns["fitness"]["jobs"] == 2
        assert campaigns["other"]["jobs"] == 1
        assert campaigns["fitness"]["usd"] == pytest.approx(0.00222 + 0.00328, rel=1e-5)

    def test_error_jobs_excluded_from_mtd(self, sqlite_session):
        """Status='error' jobs must not be counted in month_to_date_usd."""
        from core.models import RenderJob
        from web.api import _compute_spend_data

        now = datetime.now(timezone.utc)
        sqlite_session.add(RenderJob(
            campaign="test",
            backend="modal",
            gpu="l4",
            duration_s=100.0,
            rate_per_s=0.000222,
            cost_estimate=0.0222,
            status="error",
            error="render failed",
            created_at=now,
            updated_at=now,
        ))
        sqlite_session.commit()

        result = _compute_spend_data(sqlite_session, months=1)
        assert result["month_to_date_usd"] == pytest.approx(0.0)

    def test_recent_jobs_list_max_20(self, sqlite_session):
        from core.models import RenderJob
        from web.api import _compute_spend_data

        now = datetime.now(timezone.utc)
        jobs = [
            RenderJob(
                campaign="bulk",
                backend="modal",
                gpu="l4",
                duration_s=1.0,
                rate_per_s=0.000222,
                cost_estimate=0.000222,
                status="ok",
                created_at=now,
                updated_at=now,
            )
            for _ in range(25)
        ]
        sqlite_session.add_all(jobs)
        sqlite_session.commit()

        result = _compute_spend_data(sqlite_session, months=1)
        assert len(result["recent"]) == 20

    def test_remaining_never_negative(self, sqlite_session):
        """remaining_credit_usd must be clamped to 0 when spend exceeds budget."""
        from core.models import RenderJob
        from web.api import _compute_spend_data

        now = datetime.now(timezone.utc)
        # Single job that costs more than the $30 budget
        sqlite_session.add(RenderJob(
            campaign="bigrun",
            backend="modal",
            gpu="a10g",
            duration_s=200000.0,
            rate_per_s=0.000306,
            cost_estimate=61.2,  # well above $30
            status="ok",
            created_at=now,
            updated_at=now,
        ))
        sqlite_session.commit()

        result = _compute_spend_data(sqlite_session, months=1)
        assert result["remaining_credit_usd"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 5. Schedule label formatting
# ---------------------------------------------------------------------------

class TestScheduleLabel:
    """_schedule_label produces correctly formatted human-readable labels."""

    def test_single_post_single_time(self):
        from web.api import _schedule_label
        label = _schedule_label(1, ["17:00"], "America/New_York")
        assert "1×/day" in label
        assert "17:00" in label
        assert "America/New_York" in label

    def test_multiple_posts_multiple_times(self):
        from web.api import _schedule_label
        label = _schedule_label(2, ["09:00", "17:00"], "UTC")
        assert "2×/day" in label
        assert "09:00" in label
        assert "17:00" in label
        assert "UTC" in label

    def test_none_posts_per_day_defaults_to_1(self):
        from web.api import _schedule_label
        label = _schedule_label(None, ["10:00"], "America/New_York")
        assert "1×/day" in label

    def test_empty_times(self):
        from web.api import _schedule_label
        label = _schedule_label(1, [], "UTC")
        # Should not raise; returns some string
        assert isinstance(label, str)


# ---------------------------------------------------------------------------
# 6. Storage R2 key helpers
# ---------------------------------------------------------------------------

class TestStorageR2Keys:
    """r2_key_for_* functions return the correct key scheme."""

    def test_r2_key_for_clip(self):
        from core.storage import r2_key_for_clip
        key = r2_key_for_clip("fitness", 42)
        assert key == "campaigns/fitness/clips/42.mp4"

    def test_r2_key_for_thumb(self):
        from core.storage import r2_key_for_thumb
        key = r2_key_for_thumb("fitness", 99)
        assert key == "campaigns/fitness/thumbs/99.jpg"

    def test_r2_key_for_meme(self):
        from core.storage import r2_key_for_meme
        key = r2_key_for_meme("my_campaign", 7)
        assert key == "campaigns/my_campaign/memes/7.png"

    def test_r2_key_for_asset(self):
        from core.storage import r2_key_for_asset
        key = r2_key_for_asset("fitness", "logo.png")
        assert key == "campaigns/fitness/assets/logo.png"

    def test_r2_key_for_raw(self):
        from core.storage import r2_key_for_raw
        key = r2_key_for_raw("fitness", "youtube:abc123")
        assert key == "campaigns/fitness/raw/youtube_abc123.mp4"

    def test_r2_key_for_raw_slash_in_id(self):
        from core.storage import r2_key_for_raw
        key = r2_key_for_raw("fitness", "tiktok/7123456789")
        assert "/" not in key.split("raw/", 1)[1], "Slashes in source_id must be sanitised"

    def test_media_ref_is_r2_true(self):
        from core.storage import media_ref_is_r2
        assert media_ref_is_r2("r2://campaigns/fitness/clips/1.mp4") is True

    def test_media_ref_is_r2_false_for_local_path(self):
        from core.storage import media_ref_is_r2
        assert media_ref_is_r2("/data/clips/clip_1.mp4") is False

    def test_media_ref_is_r2_false_for_empty(self):
        from core.storage import media_ref_is_r2
        assert media_ref_is_r2("") is False
