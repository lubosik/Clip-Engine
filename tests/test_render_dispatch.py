"""
tests/test_render_dispatch.py — Unit tests for the render dispatch layer.

Covers (no network / GPU calls):
  1. Backend selection logic — all RENDER_BACKEND env permutations
  2. Job dict builder — shape and required fields
  3. Spend guard math — estimate + abort condition
  4. Demo channel routing — scheduler uses test_channels when mode='demo'
  5. RenderJob insertion on local render (SQLite in-memory)
"""

from __future__ import annotations

import os
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helper: minimal CampaignConfig-like object for job dict building
# ---------------------------------------------------------------------------

def _make_cfg(campaign="fitness", mode="demo"):
    """Return a minimal namespace mimicking CampaignConfig."""
    captions = types.SimpleNamespace(
        font=None,
        base_color="#FFFFFF",
        highlight_color="#00E5FF",
        outline_color="#000000",
        outline_px=6,
        position="upper_mid",
        max_words_per_line=4,
    )
    hook = types.SimpleNamespace(
        enabled=True,
        show_seconds=[0, 8],
        box_color="#111111CC",
    )
    watermark = types.SimpleNamespace(image=None, position="center", opacity=0.18, scale=0.5)
    corner_badge = types.SimpleNamespace(image=None, position="top_right", opacity=1.0, scale=0.12)
    outro = types.SimpleNamespace(enabled=False, clip=None, audio="keep")
    lower_third = types.SimpleNamespace(show_source_handle=True, format="via @{source_handle}")
    template = types.SimpleNamespace(
        resolution=[1080, 1920],
        captions=captions,
        hook=hook,
        watermark=watermark,
        corner_badge=corner_badge,
        outro=outro,
        lower_third=lower_third,
    )
    destinations = types.SimpleNamespace(
        postiz_channels=["ch1", "ch2"],
        autopost=False,
        caption_template="{hook} — via @{source_handle} {hashtags}",
        hashtags=["#fitness"],
    )
    demo_cfg = types.SimpleNamespace(test_channels=["test_ch_1"])
    return types.SimpleNamespace(
        name=campaign,
        mode=mode,
        template=template,
        destinations=destinations,
        demo=demo_cfg,
    )


def _make_source_meta():
    return {
        "source_id": "youtube:abc123",
        "platform": "youtube",
        "url": "https://youtube.com/watch?v=abc123",
        "author_handle": "testchannel",
        "channelName": "Test Channel",
    }


def _make_clip_candidate():
    return {
        "start": 10.0,
        "end": 35.0,
        "hook": "This is incredible!",
        "score": 0.88,
        "reason": "High engagement moment",
    }


# ---------------------------------------------------------------------------
# 1. Backend selection logic
# ---------------------------------------------------------------------------

class TestBackendSelection:
    """select_backend() returns 'modal' or 'local' based on env + credentials."""

    def _call(self, monkeypatch, render_backend, has_env_creds=False, has_toml=False, r2_enabled=True):
        """Call select_backend with the given env configuration."""
        from core.settings import get_settings

        monkeypatch.setenv("RENDER_BACKEND", render_backend)
        if has_env_creds:
            monkeypatch.setenv("MODAL_TOKEN_ID", "tok_test")
            monkeypatch.setenv("MODAL_TOKEN_SECRET", "sec_test")
        else:
            monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
            monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)

        if r2_enabled:
            monkeypatch.setenv("R2_BUCKET", "test-bucket")
            monkeypatch.setenv("R2_ENDPOINT", "https://test.r2.cloudflarestorage.com")
            monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
            monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
        else:
            for k in ["R2_BUCKET", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]:
                monkeypatch.delenv(k, raising=False)

        get_settings.cache_clear()

        toml_path = Path("~/.modal.toml").expanduser()
        with patch.object(Path, "exists", lambda p: has_toml if str(p) == str(toml_path) else p._orig_exists()):
            from producer.render_dispatch import select_backend
            return select_backend()

    def _call_simple(self, monkeypatch, render_backend, has_env_creds=False, r2_enabled=True):
        """Simpler call — patches both credential check and r2_enabled on the Settings object."""
        import types as _types
        from core.settings import get_settings

        monkeypatch.setenv("RENDER_BACKEND", render_backend)
        get_settings.cache_clear()

        # Build a fake settings object so tests are independent of .env / env vars
        fake_s = _types.SimpleNamespace(
            render_backend=render_backend,
            modal_token_id="tok" if has_env_creds else None,
            modal_token_secret="sec" if has_env_creds else None,
            r2_enabled=r2_enabled,
            r2_bucket="test-bucket" if r2_enabled else None,
            r2_endpoint="https://test.r2.cloudflarestorage.com" if r2_enabled else None,
            r2_access_key_id="key" if r2_enabled else None,
            r2_secret_access_key="secret" if r2_enabled else None,
        )

        with patch("producer.render_dispatch._modal_credentials_present", return_value=has_env_creds):
            with patch("core.settings.get_settings", return_value=fake_s):
                from producer.render_dispatch import select_backend
                return select_backend()

    def test_local_forced(self, monkeypatch):
        result = self._call_simple(monkeypatch, "local", has_env_creds=True)
        assert result == "local"

    def test_local_forced_no_creds(self, monkeypatch):
        result = self._call_simple(monkeypatch, "local", has_env_creds=False)
        assert result == "local"

    def test_modal_forced_with_creds(self, monkeypatch):
        result = self._call_simple(monkeypatch, "modal", has_env_creds=True)
        assert result == "modal"

    def test_modal_forced_no_creds_raises(self, monkeypatch):
        from core.settings import get_settings
        monkeypatch.setenv("RENDER_BACKEND", "modal")
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
        get_settings.cache_clear()

        with patch("producer.render_dispatch._modal_credentials_present", return_value=False):
            from producer.render_dispatch import select_backend
            with pytest.raises(RuntimeError, match="MODAL_TOKEN_ID"):
                select_backend()

    def test_auto_with_creds_and_r2_selects_modal(self, monkeypatch):
        result = self._call_simple(monkeypatch, "auto", has_env_creds=True, r2_enabled=True)
        assert result == "modal"

    def test_auto_with_creds_no_r2_selects_local(self, monkeypatch):
        result = self._call_simple(monkeypatch, "auto", has_env_creds=True, r2_enabled=False)
        assert result == "local"

    def test_auto_no_creds_selects_local(self, monkeypatch):
        result = self._call_simple(monkeypatch, "auto", has_env_creds=False, r2_enabled=True)
        assert result == "local"

    def test_auto_no_creds_no_r2_selects_local(self, monkeypatch):
        result = self._call_simple(monkeypatch, "auto", has_env_creds=False, r2_enabled=False)
        assert result == "local"

    def teardown_method(self, method):
        from core.settings import get_settings
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 2. Job dict builder shape
# ---------------------------------------------------------------------------

class TestJobDictBuilder:
    """build_job_dict returns a correctly shaped dict conforming to §4."""

    def test_required_top_level_keys(self):
        from producer.render_dispatch import build_job_dict
        cfg = _make_cfg()
        source_meta = _make_source_meta()
        clip = _make_clip_candidate()
        asset_keys = {"font": None, "watermark": None, "badge": None, "outro": None}

        job = build_job_dict(
            cfg=cfg,
            source_meta=source_meta,
            clip_candidate=clip,
            asset_r2_keys=asset_keys,
            raw_r2_key="campaigns/fitness/raw/abc123.mp4",
            output_video_key="campaigns/fitness/clips/x.mp4",
            output_thumb_key="campaigns/fitness/thumbs/x.jpg",
        )

        required = {"campaign", "mode", "source", "start", "end", "template",
                    "hook", "source_handle", "words", "asset_keys", "output"}
        assert required.issubset(job.keys()), f"Missing keys: {required - job.keys()}"

    def test_campaign_and_mode_correct(self):
        from producer.render_dispatch import build_job_dict
        cfg = _make_cfg(campaign="testcampaign", mode="production")
        job = build_job_dict(
            cfg=cfg,
            source_meta=_make_source_meta(),
            clip_candidate=_make_clip_candidate(),
            asset_r2_keys={"font": None, "watermark": None, "badge": None, "outro": None},
            raw_r2_key=None,
            output_video_key="campaigns/testcampaign/clips/abc.mp4",
            output_thumb_key="campaigns/testcampaign/thumbs/abc.jpg",
        )
        assert job["campaign"] == "testcampaign"
        assert job["mode"] == "production"

    def test_source_has_r2_raw_key_and_url(self):
        from producer.render_dispatch import build_job_dict
        cfg = _make_cfg()
        raw_key = "campaigns/fitness/raw/youtube_abc123.mp4"
        job = build_job_dict(
            cfg=cfg,
            source_meta=_make_source_meta(),
            clip_candidate=_make_clip_candidate(),
            asset_r2_keys={"font": None, "watermark": None, "badge": None, "outro": None},
            raw_r2_key=raw_key,
            output_video_key="campaigns/fitness/clips/z.mp4",
            output_thumb_key="campaigns/fitness/thumbs/z.jpg",
        )
        assert job["source"]["r2_raw_key"] == raw_key
        assert "url" in job["source"]

    def test_start_end_are_floats(self):
        from producer.render_dispatch import build_job_dict
        cfg = _make_cfg()
        clip = {"start": 10, "end": 35, "hook": "test", "score": 0.9, "reason": ""}
        job = build_job_dict(
            cfg=cfg,
            source_meta=_make_source_meta(),
            clip_candidate=clip,
            asset_r2_keys={"font": None, "watermark": None, "badge": None, "outro": None},
            raw_r2_key=None,
            output_video_key="k1.mp4",
            output_thumb_key="k1.jpg",
        )
        assert isinstance(job["start"], float)
        assert isinstance(job["end"], float)
        assert job["start"] == 10.0
        assert job["end"] == 35.0

    def test_output_keys_present(self):
        from producer.render_dispatch import build_job_dict
        cfg = _make_cfg()
        vkey = "campaigns/fitness/clips/abc.mp4"
        tkey = "campaigns/fitness/thumbs/abc.jpg"
        job = build_job_dict(
            cfg=cfg,
            source_meta=_make_source_meta(),
            clip_candidate=_make_clip_candidate(),
            asset_r2_keys={"font": None, "watermark": None, "badge": None, "outro": None},
            raw_r2_key=None,
            output_video_key=vkey,
            output_thumb_key=tkey,
        )
        assert job["output"]["video_key"] == vkey
        assert job["output"]["thumb_key"] == tkey

    def test_template_shape(self):
        from producer.render_dispatch import build_job_dict
        cfg = _make_cfg()
        job = build_job_dict(
            cfg=cfg,
            source_meta=_make_source_meta(),
            clip_candidate=_make_clip_candidate(),
            asset_r2_keys={"font": None, "watermark": None, "badge": None, "outro": None},
            raw_r2_key=None,
            output_video_key="k.mp4",
            output_thumb_key="k.jpg",
        )
        tmpl = job["template"]
        assert "resolution" in tmpl
        assert "captions" in tmpl
        assert "hook" in tmpl
        assert "watermark" in tmpl
        assert "corner_badge" in tmpl
        assert "outro" in tmpl
        assert "lower_third" in tmpl

    def test_words_is_none_by_default(self):
        """Modal worker always runs faster-whisper; words=None in job dict."""
        from producer.render_dispatch import build_job_dict
        cfg = _make_cfg()
        job = build_job_dict(
            cfg=cfg,
            source_meta=_make_source_meta(),
            clip_candidate=_make_clip_candidate(),
            asset_r2_keys={"font": None, "watermark": None, "badge": None, "outro": None},
            raw_r2_key=None,
            output_video_key="k.mp4",
            output_thumb_key="k.jpg",
        )
        assert job["words"] is None

    def test_source_handle_from_author_handle(self):
        from producer.render_dispatch import build_job_dict
        cfg = _make_cfg()
        source = _make_source_meta()
        source["author_handle"] = "myfitnessguy"
        job = build_job_dict(
            cfg=cfg,
            source_meta=source,
            clip_candidate=_make_clip_candidate(),
            asset_r2_keys={"font": None, "watermark": None, "badge": None, "outro": None},
            raw_r2_key=None,
            output_video_key="k.mp4",
            output_thumb_key="k.jpg",
        )
        assert job["source_handle"] == "myfitnessguy"


# ---------------------------------------------------------------------------
# 3. Spend guard math
# ---------------------------------------------------------------------------

class TestSpendGuardMath:
    """estimate_modal_batch_cost uses avg of last 20 OK jobs; falls back to $0.03."""

    @pytest.fixture
    def sqlite_session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from core.models import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()
        engine.dispose()

    def test_no_history_uses_fallback(self, sqlite_session):
        from producer.render_dispatch import estimate_modal_batch_cost, MODAL_DEFAULT_COST_PER_CLIP
        estimate = estimate_modal_batch_cost(5, sqlite_session)
        assert estimate == pytest.approx(5 * MODAL_DEFAULT_COST_PER_CLIP)

    def test_with_history_uses_avg(self, sqlite_session):
        from core.models import RenderJob
        from producer.render_dispatch import estimate_modal_batch_cost

        now = datetime.now(timezone.utc)
        for cost in [0.02, 0.04, 0.06]:
            sqlite_session.add(RenderJob(
                campaign="fitness", backend="modal", gpu="l4",
                duration_s=100.0, rate_per_s=0.000222, cost_estimate=cost,
                status="ok", created_at=now, updated_at=now,
            ))
        sqlite_session.commit()

        avg = (0.02 + 0.04 + 0.06) / 3
        estimate = estimate_modal_batch_cost(4, sqlite_session)
        assert estimate == pytest.approx(4 * avg, rel=1e-5)

    def test_error_jobs_excluded_from_avg(self, sqlite_session):
        """Status='error' jobs must not affect the cost average."""
        from core.models import RenderJob
        from producer.render_dispatch import estimate_modal_batch_cost, MODAL_DEFAULT_COST_PER_CLIP

        now = datetime.now(timezone.utc)
        # Only an error job exists → should fall back to default
        sqlite_session.add(RenderJob(
            campaign="fitness", backend="modal", gpu="l4",
            duration_s=100.0, rate_per_s=0.000222, cost_estimate=99.0,
            status="error", created_at=now, updated_at=now,
        ))
        sqlite_session.commit()

        estimate = estimate_modal_batch_cost(3, sqlite_session)
        assert estimate == pytest.approx(3 * MODAL_DEFAULT_COST_PER_CLIP)

    def test_estimate_zero_clips_is_zero(self, sqlite_session):
        from producer.render_dispatch import estimate_modal_batch_cost
        assert estimate_modal_batch_cost(0, sqlite_session) == 0.0

    def test_abort_condition_exceeds_limit(self, sqlite_session):
        """When estimate > max_modal_spend, the caller should abort."""
        from producer.render_dispatch import estimate_modal_batch_cost, MODAL_DEFAULT_COST_PER_CLIP

        n_clips = 10
        estimate = estimate_modal_batch_cost(n_clips, sqlite_session)
        max_spend = n_clips * MODAL_DEFAULT_COST_PER_CLIP - 0.001  # just below

        assert estimate > max_spend, "Estimate should exceed max_spend to trigger abort"

    def test_local_backend_cost_is_zero(self, sqlite_session):
        """Local renders have cost_estimate=0; should not inflate avg."""
        from core.models import RenderJob
        from producer.render_dispatch import estimate_modal_batch_cost, MODAL_DEFAULT_COST_PER_CLIP

        now = datetime.now(timezone.utc)
        # Local job: backend='local', cost=0.0 (should not be counted)
        sqlite_session.add(RenderJob(
            campaign="fitness", backend="local", gpu=None,
            duration_s=30.0, rate_per_s=0.0, cost_estimate=0.0,
            status="ok", created_at=now, updated_at=now,
        ))
        sqlite_session.commit()

        # No modal jobs → should use fallback
        estimate = estimate_modal_batch_cost(2, sqlite_session)
        assert estimate == pytest.approx(2 * MODAL_DEFAULT_COST_PER_CLIP)


# ---------------------------------------------------------------------------
# 4. Demo channel routing selection (scheduler)
# ---------------------------------------------------------------------------

class TestDemoChannelRouting:
    """Scheduler should route demo clips to test_channels when non-empty."""

    def _make_demo_clip(self, mode: str, clip_id: int = 1):
        return types.SimpleNamespace(
            id=clip_id,
            mode=mode,
            postiz_post_ids=None,
            campaign="fitness",
            file_path="/tmp/test.mp4",
            hook="test hook",
            source_rel=None,
        )

    def _make_configs(self, test_channels=None, autopost=False):
        """Return configs dict mimicking load_enabled_campaigns output."""
        demo_cfg = types.SimpleNamespace(test_channels=test_channels or [])
        dest = types.SimpleNamespace(
            postiz_channels=["live_channel_1", "live_channel_2"],
            autopost=autopost,
            caption_template="{hook}",
            hashtags=[],
            schedule=types.SimpleNamespace(
                posts_per_day=1,
                times=["17:00"],
                timezone="America/New_York",
            ),
        )
        cfg = types.SimpleNamespace(
            name="fitness",
            mode="demo",
            demo=demo_cfg,
            destinations=dest,
        )
        return {"fitness": cfg}

    def test_demo_mode_with_test_channels_uses_test_channels(self):
        """clip.mode='demo' + test_channels non-empty → scheduler uses test_channels."""
        from scheduler.schedule import _process_clip

        clip = self._make_demo_clip("demo")
        configs = self._make_configs(test_channels=["test_ch_1"])

        posted_to: list[str] = []
        calls_logged: list[str] = []

        class MockPostiz:
            def create_post(self, channel, caption, video_path, schedule_at, draft):
                posted_to.append(channel)
                return {"id": "post_123"}

        with (
            patch("scheduler.schedule._load_taken_slots", return_value=[]),
            patch("scheduler.schedule._persist_scheduled"),
            patch("pathlib.Path.exists", return_value=True),
        ):
            mock_postiz = MockPostiz()
            _process_clip(clip, configs, mock_postiz, dry_run=False)

        # Should have posted to test channel only
        assert "test_ch_1" in posted_to
        assert "live_channel_1" not in posted_to
        assert "live_channel_2" not in posted_to

    def test_demo_mode_no_test_channels_uses_live_channels(self):
        """clip.mode='demo' + test_channels=[] → scheduler uses live channels."""
        from scheduler.schedule import _process_clip

        clip = self._make_demo_clip("demo")
        configs = self._make_configs(test_channels=[])

        posted_to: list[str] = []

        class MockPostiz:
            def create_post(self, channel, caption, video_path, schedule_at, draft):
                posted_to.append(channel)
                return {"id": "post_456"}

        with (
            patch("scheduler.schedule._load_taken_slots", return_value=[]),
            patch("scheduler.schedule._persist_scheduled"),
            patch("pathlib.Path.exists", return_value=True),
        ):
            _process_clip(clip, configs, MockPostiz(), dry_run=False)

        assert "live_channel_1" in posted_to

    def test_production_mode_always_uses_live_channels(self):
        """clip.mode='production' → always uses live channels regardless of test_channels."""
        from scheduler.schedule import _process_clip

        clip = self._make_demo_clip("production")
        configs = self._make_configs(test_channels=["test_ch_1"])

        posted_to: list[str] = []

        class MockPostiz:
            def create_post(self, channel, caption, video_path, schedule_at, draft):
                posted_to.append(channel)
                return {"id": "post_789"}

        with (
            patch("scheduler.schedule._load_taken_slots", return_value=[]),
            patch("scheduler.schedule._persist_scheduled"),
            patch("pathlib.Path.exists", return_value=True),
        ):
            _process_clip(clip, configs, MockPostiz(), dry_run=False)

        assert "live_channel_1" in posted_to
        assert "test_ch_1" not in posted_to

    def test_demo_clips_post_as_drafts(self):
        """Demo clips must always be drafts (even if autopost=True)."""
        from scheduler.schedule import _process_clip

        clip = self._make_demo_clip("demo")
        configs = self._make_configs(test_channels=["test_ch"], autopost=True)

        draft_values: list[bool] = []

        class MockPostiz:
            def create_post(self, channel, caption, video_path, schedule_at, draft):
                draft_values.append(draft)
                return {"id": "post_draft"}

        with (
            patch("scheduler.schedule._load_taken_slots", return_value=[]),
            patch("scheduler.schedule._persist_scheduled"),
            patch("pathlib.Path.exists", return_value=True),
        ):
            _process_clip(clip, configs, MockPostiz(), dry_run=False)

        assert all(draft_values), "All demo clip posts must be drafts"


# ---------------------------------------------------------------------------
# 5. RenderJob insertion on local render (SQLite in-memory)
# ---------------------------------------------------------------------------

class TestRenderJobInsertion:
    """_insert_render_job populates RenderJob rows correctly."""

    @pytest.fixture
    def sqlite_session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from core.models import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()
        engine.dispose()

    def test_local_render_job_inserted(self, sqlite_session):
        """Local backend: cost=0.0, gpu=None."""
        from producer.render_dispatch import _insert_render_job
        from core.models import RenderJob

        _insert_render_job(
            sqlite_session,
            campaign="fitness",
            backend="local",
            gpu=None,
            duration_s=12.5,
            status="ok",
            error=None,
        )
        sqlite_session.commit()

        jobs = sqlite_session.query(RenderJob).all()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.campaign == "fitness"
        assert job.backend == "local"
        assert job.gpu is None
        assert job.cost_estimate == pytest.approx(0.0)
        assert job.rate_per_s == pytest.approx(0.0)
        assert job.status == "ok"
        assert job.error is None
        assert job.clip_id is None

    def test_modal_render_job_inserted_with_cost(self, sqlite_session):
        """Modal backend: cost computed from GPU rate × duration."""
        from producer.render_dispatch import _insert_render_job
        from core.models import RenderJob
        from core.modal_costs import estimate_cost, rate_for

        _insert_render_job(
            sqlite_session,
            campaign="fitness",
            backend="modal",
            gpu="l4",
            duration_s=60.0,
            status="ok",
            error=None,
        )
        sqlite_session.commit()

        jobs = sqlite_session.query(RenderJob).all()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.backend == "modal"
        assert job.gpu == "l4"
        assert job.rate_per_s == pytest.approx(rate_for("l4"))
        assert job.cost_estimate == pytest.approx(estimate_cost("l4", 60.0))

    def test_error_render_job_inserted(self, sqlite_session):
        """Error renders are recorded with status='error'."""
        from producer.render_dispatch import _insert_render_job
        from core.models import RenderJob

        _insert_render_job(
            sqlite_session,
            campaign="fitness",
            backend="modal",
            gpu="t4",
            duration_s=5.0,
            status="error",
            error="nvenc encoder not available",
        )
        sqlite_session.commit()

        jobs = sqlite_session.query(RenderJob).all()
        assert len(jobs) == 1
        assert jobs[0].status == "error"
        assert "nvenc" in (jobs[0].error or "")

    def test_month_to_date_excludes_errors(self, sqlite_session):
        """month_to_date_modal_spend only sums OK jobs."""
        from core.models import RenderJob
        from producer.render_dispatch import month_to_date_modal_spend

        now = datetime.now(timezone.utc)
        sqlite_session.add(RenderJob(
            campaign="fitness", backend="modal", gpu="l4",
            duration_s=10.0, rate_per_s=0.000222, cost_estimate=0.00222,
            status="ok", created_at=now, updated_at=now,
        ))
        sqlite_session.add(RenderJob(
            campaign="fitness", backend="modal", gpu="l4",
            duration_s=10.0, rate_per_s=0.000222, cost_estimate=5.0,
            status="error", created_at=now, updated_at=now,
        ))
        sqlite_session.commit()

        mtd = month_to_date_modal_spend(sqlite_session)
        assert mtd == pytest.approx(0.00222)
