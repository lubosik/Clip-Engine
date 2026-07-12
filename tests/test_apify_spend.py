"""Tests for the Apify spend ledger + real-cost tracking (migration 004)."""

from __future__ import annotations

from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixtures — file-based SQLite so every connection shares the store
# ---------------------------------------------------------------------------

@pytest.fixture()
def sqlite_db(tmp_path, monkeypatch):
    """Point core.db at a throwaway SQLite file with the full schema."""
    import core.db as core_db
    from core.models import Base
    from sqlalchemy import create_engine

    db_path = tmp_path / "test.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)

    # Reset cached engine/session factory
    core_db._engine = None
    core_db._SessionLocal = None

    from core.settings import get_settings
    get_settings.cache_clear()

    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()

    yield url

    core_db._engine = None
    core_db._SessionLocal = None
    get_settings.cache_clear()


class _FakeDataset:
    def __init__(self, items):
        self._items = items

    def iterate_items(self, **kwargs):
        limit = kwargs.get("limit")
        items = self._items[:limit] if limit else self._items
        yield from items


class _FakeActor:
    def __init__(self, run):
        self._run = run

    def call(self, run_input):
        return self._run


class _FakeClient:
    def __init__(self, run, items):
        self._run = run
        self._items = items

    def actor(self, actor_id):
        return _FakeActor(self._run)

    def dataset(self, dataset_id):
        return _FakeDataset(self._items)


def _make_apify(run: dict, items: list):
    from core.apify import Apify

    apify = Apify()
    apify._client = _FakeClient(run, items)
    return apify


# ---------------------------------------------------------------------------
# Apify.run — cost accumulation + ledger
# ---------------------------------------------------------------------------

class TestApifyCostTracking:
    def test_cost_accumulates_across_runs(self, sqlite_db):
        run = {
            "id": "r1", "status": "SUCCEEDED", "usageTotalUsd": 0.05,
            "defaultDatasetId": "d1",
        }
        apify = _make_apify(run, [{"title": "a"}, {"title": "b"}])
        apify.run("some/actor", {}, campaign="pep", kind="discovery")
        apify.run("some/actor", {}, campaign="pep", kind="discovery")
        assert apify.total_cost_usd == pytest.approx(0.10)
        assert apify.runs_count == 2

    def test_missing_cost_counts_run_but_not_dollars(self, sqlite_db):
        run = {"id": "r1", "status": "SUCCEEDED", "defaultDatasetId": "d1"}
        apify = _make_apify(run, [])
        apify.run("some/actor", {})
        assert apify.total_cost_usd == 0.0
        assert apify.runs_count == 1

    def test_ledger_row_written(self, sqlite_db):
        from core.db import get_session
        from core.models import ApifyRun

        run = {
            "id": "run-xyz", "status": "SUCCEEDED", "usageTotalUsd": 0.031,
            "defaultDatasetId": "d1",
        }
        apify = _make_apify(run, [{"title": "v1"}, {"title": "v2"}, {"title": "v3"}])
        apify.run("streamers/youtube-scraper", {}, campaign="peptides", kind="discovery")

        with get_session() as session:
            rows = session.query(ApifyRun).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.run_id == "run-xyz"
            assert row.actor_id == "streamers/youtube-scraper"
            assert row.campaign == "peptides"
            assert row.kind == "discovery"
            assert row.items == 3
            assert row.cost_usd == pytest.approx(0.031)
            assert row.status == "SUCCEEDED"

    def test_real_record_ledger_swallows_db_errors(self, monkeypatch):
        """_record_ledger itself must never raise, even with no DB at all."""
        from core.apify import Apify

        monkeypatch.setenv("DATABASE_URL", "postgresql://nouser:nopass@127.0.0.1:1/none")
        import core.db as core_db
        core_db._engine = None
        core_db._SessionLocal = None
        from core.settings import get_settings
        get_settings.cache_clear()

        # Must not raise
        Apify._record_ledger(
            run_id="r", actor_id="a/b", campaign=None, kind="other",
            items=0, cost_usd=None, status=None,
        )

        core_db._engine = None
        core_db._SessionLocal = None
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# /api/spend apify section
# ---------------------------------------------------------------------------

class TestSpendApiApifySection:
    def test_compute_apify_spend_aggregates(self, sqlite_db):
        from datetime import datetime, timedelta, timezone

        from core.db import get_session
        from core.models import ApifyRun
        from web.api import _compute_apify_spend

        with get_session() as session:
            session.add_all([
                ApifyRun(run_id="1", actor_id="a/yt", campaign="pep",
                         kind="discovery", items=100, cost_usd=0.30, status="SUCCEEDED"),
                ApifyRun(run_id="2", actor_id="a/tr", campaign="pep",
                         kind="transcript", items=1, cost_usd=0.01, status="SUCCEEDED"),
                ApifyRun(run_id="3", actor_id="a/yt", campaign="pep",
                         kind="discovery", items=50, cost_usd=0.15, status="SUCCEEDED"),
            ])
            session.commit()

        since = datetime.now(timezone.utc) - timedelta(days=30)
        with get_session() as session:
            data = _compute_apify_spend(session, since)

        assert data["total_usd"] == pytest.approx(0.46)
        assert data["runs"] == 3
        assert data["items"] == 151
        kinds = {k["kind"]: k for k in data["by_kind"]}
        assert kinds["discovery"]["usd"] == pytest.approx(0.45)
        assert kinds["discovery"]["items"] == 150
        # $0.45 discovery / 150 videos = $0.003/video
        assert data["avg_cost_per_video_usd"] == pytest.approx(0.003)

    def test_compute_apify_spend_empty(self, sqlite_db):
        from datetime import datetime, timedelta, timezone

        from core.db import get_session
        from web.api import _compute_apify_spend

        since = datetime.now(timezone.utc) - timedelta(days=30)
        with get_session() as session:
            data = _compute_apify_spend(session, since)
        assert data["total_usd"] == 0.0
        assert data["avg_cost_per_video_usd"] is None


# ---------------------------------------------------------------------------
# Backlog-first discovery skip
# ---------------------------------------------------------------------------

class TestBacklogSkip:
    def test_count_backlog_sources(self, sqlite_db):
        from core.db import ensure_campaign, get_session
        from core.models import Source
        from producer.run import _count_backlog_sources

        with get_session() as session:
            ensure_campaign(session, "pep", enabled=True, config_snapshot=None)
            for i, status in enumerate(
                ["pending", "selected", "partially_done", "done", "pending"]
            ):
                session.add(Source(
                    source_id=f"s{i}", campaign="pep", platform="youtube",
                    url=f"https://y/{i}", title=f"t{i}", status=status,
                ))
            session.commit()

        with get_session() as session:
            assert _count_backlog_sources(session, "pep") == 4
            assert _count_backlog_sources(session, "other") == 0

    def test_sources_config_default_threshold(self):
        from core.config import SourcesConfig, YouTubeSourceConfig

        cfg = SourcesConfig(youtube=YouTubeSourceConfig())
        assert cfg.skip_discovery_backlog == 20

    def test_results_per_search_config(self):
        from core.config import YouTubeSourceConfig, TikTokSourceConfig

        assert YouTubeSourceConfig().results_per_search == 20
        assert YouTubeSourceConfig(results_per_search=10).results_per_search == 10
        assert TikTokSourceConfig().results_per_search == 20
        with pytest.raises(Exception):
            YouTubeSourceConfig(results_per_search=0)
