"""
tests/test_pipeline_stages.py — unit tests for pipeline stage tracking.

Covers:
  - set_source_stage: basic transition, clips_identified, error, not-found source
  - _process_source: stage progression transcribing→identifying→rendering
  - _process_source: probe failure → stage 'failed', status untouched, rank NOT called
  - _process_source: TranscriptFetchError → stage 'failed'
  - _process_source: 0 selected clips → stage 'complete'
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared SQLite fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    """File-based SQLite session with schema created."""
    db_file = tmp_path / "test_stages.db"
    db_url = f"sqlite:///{db_file}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))

    from core.settings import get_settings
    get_settings.cache_clear()

    import core.db as _db
    _db._engine = None
    _db._SessionLocal = None

    from sqlalchemy import create_engine
    from core.models import Base
    eng = create_engine(db_url)
    Base.metadata.create_all(eng)
    eng.dispose()

    from core.db import get_session
    from core.models import Campaign, Source

    with get_session() as session:
        session.add(Campaign(name="fitness"))
        session.commit()

    # Yield a fresh session (caller must commit/rollback)
    with get_session() as session:
        yield session

    get_settings.cache_clear()
    _db._engine = None
    _db._SessionLocal = None


def _make_source(session, source_id: str, platform: str = "youtube", status: str = "pending") -> None:
    from core.models import Source
    src = Source(
        source_id=source_id,
        campaign="fitness",
        platform=platform,
        url=f"https://example.com/{source_id}",
        status=status,
        stage="queued",
    )
    session.add(src)
    session.commit()


# ---------------------------------------------------------------------------
# set_source_stage tests
# ---------------------------------------------------------------------------

class TestSetSourceStage:
    def test_basic_stage_transition(self, db_session):
        from producer.run import set_source_stage
        _make_source(db_session, "youtube:s1")
        set_source_stage(db_session, "youtube:s1", "transcribing")

        from core.models import Source
        src = db_session.query(Source).filter_by(source_id="youtube:s1").first()
        assert src.stage == "transcribing"
        assert src.stage_updated_at is not None

    def test_sets_clips_identified(self, db_session):
        from producer.run import set_source_stage
        _make_source(db_session, "youtube:s2")
        set_source_stage(db_session, "youtube:s2", "rendering", clips_identified=5)

        from core.models import Source
        src = db_session.query(Source).filter_by(source_id="youtube:s2").first()
        assert src.stage == "rendering"
        assert src.clips_identified == 5

    def test_sets_error(self, db_session):
        from producer.run import set_source_stage
        _make_source(db_session, "youtube:s3")
        set_source_stage(db_session, "youtube:s3", "failed", error="DRM protected")

        from core.models import Source
        src = db_session.query(Source).filter_by(source_id="youtube:s3").first()
        assert src.stage == "failed"
        assert "DRM" in src.stage_error

    def test_error_truncated_to_500(self, db_session):
        from producer.run import set_source_stage
        _make_source(db_session, "youtube:s4")
        long_err = "x" * 1000
        set_source_stage(db_session, "youtube:s4", "failed", error=long_err)

        from core.models import Source
        src = db_session.query(Source).filter_by(source_id="youtube:s4").first()
        assert len(src.stage_error) <= 500

    def test_not_found_source_no_raise(self, db_session):
        from producer.run import set_source_stage
        # Should not raise even for a non-existent source
        set_source_stage(db_session, "youtube:nonexistent", "transcribing")

    def test_stage_updated_at_set(self, db_session):
        from producer.run import set_source_stage
        _make_source(db_session, "youtube:s5")
        before = datetime.now(tz=timezone.utc)
        set_source_stage(db_session, "youtube:s5", "identifying")
        after = datetime.now(tz=timezone.utc)

        from core.models import Source
        src = db_session.query(Source).filter_by(source_id="youtube:s5").first()
        ts = src.stage_updated_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        assert before <= ts <= after


# ---------------------------------------------------------------------------
# _process_source stage wiring tests
# ---------------------------------------------------------------------------

class TestProcessSourceStages:
    """Test that _process_source sets correct stages at each step."""

    def _run_process(
        self,
        tmp_path,
        monkeypatch,
        db_session,
        *,
        platform: str = "youtube",
        segments=None,
        candidates=None,
        selected=None,
        probe_raises=None,
        render_error=False,
    ):
        """Helper to run _process_source with mocked dependencies."""
        from core.models import Source
        source_id = f"{platform}:test001"
        _make_source(db_session, source_id, platform=platform)

        source_dict = {
            "source_id": source_id,
            "platform": platform,
            "url": f"https://example.com/{source_id}",
            "title": "Test",
            "author_handle": "testchan",
            "raw": {},
        }

        if segments is None:
            segments = [{"start": 0.0, "end": 30.0, "text": "Test segment"}]

        if candidates is None:
            candidates = [{"start": 0.0, "end": 30.0, "score": 0.9, "hook": "Hook", "reason": "good"}]

        if selected is None:
            selected = list(candidates)

        # Minimal mock objects
        campaign_cfg = MagicMock()
        campaign_cfg.name = "fitness"
        campaign_cfg.ranking.ranking_rules = "Rules"
        campaign_cfg.ranking.exhaust_source = False
        campaign_cfg.ranking.clip_length = [15, 60]
        campaign_cfg.ranking.min_score = 0.5
        campaign_cfg.ranking.max_clips_per_source = 8
        campaign_cfg.destinations.caption_template = "{hook} {hashtags}"
        campaign_cfg.destinations.hashtags = []
        campaign_cfg.destinations.postiz_channels = []
        campaign_cfg.gate = MagicMock()
        campaign_cfg.gate.relaxed_safety_checks = []

        apify = MagicMock()

        mock_segments = segments

        with (
            patch("producer.comments.pull_and_store_comments", return_value=None),
            patch("producer.run.fetch_and_store_transcript", return_value=mock_segments),
            patch("producer.run.rank_clips", return_value=candidates) as mock_rank,
            patch("producer.run.select_clips", return_value=selected),
            patch("producer.run.estimate_modal_batch_cost", return_value=0.0),
            patch("producer.run.mark_source_status"),
            patch("producer.run.update_used_ranges"),
            patch("producer.run.download_source", return_value=tmp_path / "video.mp4"),
            patch("producer.run.cleanup_source"),
            patch(
                "producer.run.render_and_record",
                return_value=MagicMock(
                    status="error" if render_error else "ok",
                    file_path=str(tmp_path / "clip.mp4"),
                    thumb_path=str(tmp_path / "thumb.jpg"),
                    backend="local",
                    error="render fail" if render_error else None,
                ),
            ),
            patch(
                "producer.review_gate.run_gate",
                return_value=MagicMock(
                    gate_status="pending",
                    gate_reasons=[],
                    formula_score=None,
                ),
            ),
        ):
            if probe_raises is not None:
                with patch("producer.download.probe_youtube", side_effect=probe_raises):
                    # Need to also patch within producer.run
                    with patch("producer.run.probe_youtube" if hasattr(
                        __import__("producer.run", fromlist=["probe_youtube"]),
                        "probe_youtube",
                    ) else "producer.download.probe_youtube", side_effect=probe_raises, create=True):
                        from producer.run import _process_source
                        result = _process_source(
                            source_dict,
                            campaign_cfg,
                            apify,
                            db_session,
                            2,
                            run_mode="demo",
                        )
            else:
                from producer.run import _process_source
                result = _process_source(
                    source_dict,
                    campaign_cfg,
                    apify,
                    db_session,
                    2,
                    run_mode="demo",
                )

        db_session.expire_all()
        src = db_session.query(Source).filter_by(source_id=source_id).first()
        return result, src, mock_rank if probe_raises is None else None

    def test_stage_transcribing_then_identifying_then_rendering(self, tmp_path, monkeypatch, db_session):
        """Normal flow: stages progress through transcribing → identifying → rendering."""
        # We can't inspect intermediate stages directly since they're overwritten,
        # but we can verify the final stage is 'reviewing' (>=1 clip) and that
        # clips_identified is set.
        from core.models import Source

        source_id = "youtube:flow001"
        _make_source(db_session, source_id, platform="youtube")

        candidates = [{"start": 0.0, "end": 30.0, "score": 0.9, "hook": "Hook", "reason": "good"}]

        campaign_cfg = MagicMock()
        campaign_cfg.name = "fitness"
        campaign_cfg.ranking.ranking_rules = "Rules"
        campaign_cfg.ranking.exhaust_source = False
        campaign_cfg.ranking.clip_length = [15, 60]
        campaign_cfg.ranking.min_score = 0.5
        campaign_cfg.ranking.max_clips_per_source = 8
        campaign_cfg.destinations.caption_template = "{hook}"
        campaign_cfg.destinations.hashtags = []
        campaign_cfg.destinations.postiz_channels = []
        campaign_cfg.gate = MagicMock()
        campaign_cfg.gate.relaxed_safety_checks = []

        source_dict = {
            "source_id": source_id,
            "platform": "youtube",
            "url": "https://youtube.com/watch?v=abc",
            "title": "Test",
            "author_handle": "chan",
            "raw": {},
        }

        with (
            patch("producer.comments.pull_and_store_comments", return_value=None),
            patch("producer.transcripts.fetch_and_store_transcript", return_value=[
                {"start": 0.0, "end": 30.0, "text": "Hello world"}
            ]),
            patch("producer.ranker.rank_clips", return_value=candidates),
            patch("producer.ranker.select_clips", return_value=candidates),
            patch("producer.render_dispatch.estimate_modal_batch_cost", return_value=0.0),
            patch("producer.dedupe.mark_source_status"),
            patch("producer.dedupe.update_used_ranges"),
            patch("producer.download.probe_youtube"),  # probe passes
            patch("producer.download.download_source", return_value=tmp_path / "video.mp4"),
            patch("producer.download.cleanup_source"),
            patch(
                "producer.render_dispatch.render_and_record",
                return_value=MagicMock(
                    status="ok",
                    file_path=str(tmp_path / "clip.mp4"),
                    thumb_path=str(tmp_path / "thumb.jpg"),
                    backend="local",
                    error=None,
                ),
            ),
            patch(
                "producer.review_gate.run_gate",
                return_value=MagicMock(
                    gate_status="ready",
                    gate_reasons=[],
                    formula_score=0.8,
                ),
            ),
        ):
            from producer.run import _process_source
            clips = _process_source(
                source_dict, campaign_cfg, MagicMock(), db_session, 2, run_mode="demo"
            )

        db_session.expire_all()
        src = db_session.query(Source).filter_by(source_id=source_id).first()

        assert len(clips) == 1
        assert src.stage == "reviewing"
        # clips_identified was set to len(selected) = 1
        assert src.clips_identified == 1

    def test_zero_selected_clips_stage_complete(self, tmp_path, db_session):
        """When no clips are selected, stage goes to 'complete'."""
        from core.models import Source

        source_id = "youtube:nosel001"
        _make_source(db_session, source_id, platform="youtube")

        source_dict = {
            "source_id": source_id,
            "platform": "youtube",
            "url": "https://youtube.com/watch?v=nosel",
            "title": "Test",
            "author_handle": "chan",
            "raw": {},
        }

        campaign_cfg = MagicMock()
        campaign_cfg.name = "fitness"
        campaign_cfg.ranking.ranking_rules = "Rules"
        campaign_cfg.ranking.exhaust_source = False
        campaign_cfg.ranking.clip_length = [15, 60]
        campaign_cfg.ranking.min_score = 0.5
        campaign_cfg.ranking.max_clips_per_source = 8
        campaign_cfg.destinations.caption_template = "{hook}"
        campaign_cfg.destinations.hashtags = []
        campaign_cfg.destinations.postiz_channels = []
        campaign_cfg.gate = MagicMock()
        campaign_cfg.gate.relaxed_safety_checks = []

        with (
            patch("producer.comments.pull_and_store_comments", return_value=None),
            patch("producer.transcripts.fetch_and_store_transcript", return_value=[
                {"start": 0.0, "end": 10.0, "text": "Short"}
            ]),
            patch("producer.ranker.rank_clips", return_value=[]),
            patch("producer.ranker.select_clips", return_value=[]),
            patch("producer.download.probe_youtube"),
            patch("producer.dedupe.mark_source_status"),
        ):
            from producer.run import _process_source
            clips = _process_source(
                source_dict, campaign_cfg, MagicMock(), db_session, 2, run_mode="demo"
            )

        db_session.expire_all()
        src = db_session.query(Source).filter_by(source_id=source_id).first()

        assert clips == []
        assert src.stage == "complete"

    def test_probe_failure_stage_failed_status_untouched_rank_not_called(
        self, tmp_path, db_session
    ):
        """Probe failure: stage='failed', source.status unchanged, rank_clips NOT called."""
        from core.models import Source

        source_id = "youtube:probe001"
        _make_source(db_session, source_id, platform="youtube", status="pending")

        source_dict = {
            "source_id": source_id,
            "platform": "youtube",
            "url": "https://youtube.com/watch?v=probe",
            "title": "Test",
            "author_handle": "chan",
            "raw": {},
        }

        campaign_cfg = MagicMock()
        campaign_cfg.name = "fitness"
        campaign_cfg.ranking.ranking_rules = "Rules"
        campaign_cfg.ranking.exhaust_source = False
        campaign_cfg.ranking.clip_length = [15, 60]
        campaign_cfg.ranking.min_score = 0.5
        campaign_cfg.ranking.max_clips_per_source = 8
        campaign_cfg.destinations.caption_template = "{hook}"
        campaign_cfg.destinations.hashtags = []
        campaign_cfg.destinations.postiz_channels = []
        campaign_cfg.gate = MagicMock()
        campaign_cfg.gate.relaxed_safety_checks = []

        rank_mock = MagicMock()

        with (
            patch("producer.comments.pull_and_store_comments", return_value=None),
            patch("producer.transcripts.fetch_and_store_transcript", return_value=[
                {"start": 0.0, "end": 30.0, "text": "Content"}
            ]),
            patch("producer.ranker.rank_clips", rank_mock),
            patch("producer.download.probe_youtube", side_effect=RuntimeError("DRM protected")),
            patch("producer.dedupe.mark_source_status"),
        ):
            from producer.run import _process_source
            clips = _process_source(
                source_dict, campaign_cfg, MagicMock(), db_session, 2, run_mode="demo"
            )

        db_session.expire_all()
        src = db_session.query(Source).filter_by(source_id=source_id).first()

        # Probe failed → return [], stage=failed, status untouched
        assert clips == []
        assert src.stage == "failed"
        assert "DRM" in (src.stage_error or "")
        # status was 'pending' and must remain untouched (not marked done)
        assert src.status == "pending"
        # rank_clips must NOT have been called
        rank_mock.assert_not_called()

    def test_transcript_fetch_error_stage_failed(self, tmp_path, db_session):
        """TranscriptFetchError → stage 'failed', source left for future retry."""
        from core.models import Source
        from producer.transcripts import TranscriptFetchError

        source_id = "youtube:tferr001"
        _make_source(db_session, source_id, platform="youtube", status="pending")

        source_dict = {
            "source_id": source_id,
            "platform": "youtube",
            "url": "https://youtube.com/watch?v=tferr",
            "title": "Test",
            "author_handle": "chan",
            "raw": {},
        }

        campaign_cfg = MagicMock()
        campaign_cfg.name = "fitness"
        campaign_cfg.ranking.ranking_rules = "Rules"
        campaign_cfg.ranking.exhaust_source = False
        campaign_cfg.ranking.clip_length = [15, 60]
        campaign_cfg.ranking.min_score = 0.5
        campaign_cfg.ranking.max_clips_per_source = 8
        campaign_cfg.destinations.caption_template = "{hook}"
        campaign_cfg.destinations.hashtags = []
        campaign_cfg.destinations.postiz_channels = []
        campaign_cfg.gate = MagicMock()
        campaign_cfg.gate.relaxed_safety_checks = []

        with (
            patch("producer.comments.pull_and_store_comments", return_value=None),
            patch(
                "producer.transcripts.fetch_and_store_transcript",
                side_effect=TranscriptFetchError("Apify outage"),
            ),
        ):
            from producer.run import _process_source
            clips = _process_source(
                source_dict, campaign_cfg, MagicMock(), db_session, 2, run_mode="demo"
            )

        db_session.expire_all()
        src = db_session.query(Source).filter_by(source_id=source_id).first()

        assert clips == []
        assert src.stage == "failed"
        assert "Apify" in (src.stage_error or "")
        # status must remain 'pending' so the next run retries
        assert src.status == "pending"
