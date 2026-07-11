"""
tests/test_sources_api.py — API-level tests for GET /api/sources.

Covers:
  - Auth required (401 without credentials)
  - Empty list when no non-pending sources exist
  - Pending-only sources excluded
  - Non-pending sources included
  - Sources with clips included even if status == 'pending'
  - clip_count and clips list are correct
  - Newest-first sort order (by processed_at / updated_at)
  - ?campaign= filter
  - ?q= search (title, author_handle)
  - YouTube thumbnail_url derived from source_id
  - TikTok thumbnail_url from videoMeta.coverUrl
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Test fixture — file-based SQLite + reset (mirrors test_gate_api.py pattern)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_sources.db"
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
    _setup = create_engine(db_url)
    Base.metadata.create_all(_setup)
    _setup.dispose()

    # Seed campaign rows (FK required)
    from core.db import get_session
    from core.models import Campaign
    with get_session() as s:
        s.add(Campaign(name="fitness"))
        s.add(Campaign(name="peptides"))
        s.commit()

    from web.api import app
    c = TestClient(app, base_url="https://testserver")
    r = c.post("/api/auth/session", headers={"Authorization": "Bearer testpass"})
    assert r.status_code == 200

    yield c

    get_settings.cache_clear()
    _db._engine = None
    _db._SessionLocal = None


def _auth():
    return {"Authorization": "Bearer testpass"}


# ---------------------------------------------------------------------------
# Helpers — insert sources and clips
# ---------------------------------------------------------------------------

def _insert_source(
    source_id: str,
    campaign: str = "fitness",
    platform: str = "youtube",
    url: str = "https://youtube.com/watch?v=test",
    title: str = "Test Video",
    author_handle: str = "testchannel",
    status: str = "done",
    processed_at: datetime | None = None,
    source_metadata: dict | None = None,
    used_ranges: list | None = None,
) -> None:
    from core.db import get_session
    from core.models import Source
    if processed_at is None:
        processed_at = datetime.now(timezone.utc)
    with get_session() as s:
        src = Source(
            source_id=source_id,
            campaign=campaign,
            platform=platform,
            url=url,
            title=title,
            author_handle=author_handle,
            status=status,
            processed_at=processed_at,
            source_metadata=source_metadata or {},
            used_ranges=used_ranges or [],
        )
        s.add(src)
        s.commit()


def _insert_clip(source_id: str, campaign: str = "fitness", hook: str = "Test hook", gate_status: str = "ready") -> str:
    from core.db import get_session
    from core.models import Clip
    with get_session() as s:
        clip = Clip(
            campaign=campaign,
            source_id=source_id,
            kind="clip",
            mode="demo",
            aspect="9:16",
            status="pending_review",
            hook=hook,
            gate_status=gate_status,
        )
        s.add(clip)
        s.commit()
        return str(clip.id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSourcesAuth:
    def test_requires_auth(self, client):
        # Clear session cookie so request is truly unauthenticated
        client.cookies.clear()
        r = client.get("/api/sources")  # no auth header, no cookie
        assert r.status_code == 401

    def test_auth_ok(self, client):
        r = client.get("/api/sources", headers=_auth())
        assert r.status_code == 200


class TestSourcesFiltering:
    def test_empty_when_no_sources(self, client):
        r = client.get("/api/sources", headers=_auth())
        assert r.json() == []

    def test_pending_source_without_clips_excluded(self, client):
        _insert_source("youtube:pending1", status="pending")
        r = client.get("/api/sources", headers=_auth())
        assert r.json() == []

    def test_done_source_included(self, client):
        _insert_source("youtube:done1", status="done")
        r = client.get("/api/sources", headers=_auth())
        assert len(r.json()) == 1
        assert r.json()[0]["source_id"] == "youtube:done1"

    def test_partially_done_source_included(self, client):
        _insert_source("youtube:partial1", status="partially_done")
        r = client.get("/api/sources", headers=_auth())
        assert len(r.json()) == 1

    def test_selected_source_included(self, client):
        _insert_source("youtube:sel1", status="selected", processed_at=None)
        r = client.get("/api/sources", headers=_auth())
        assert len(r.json()) == 1

    def test_pending_source_with_clips_included(self, client):
        _insert_source("youtube:pend_with_clip", status="pending")
        _insert_clip("youtube:pend_with_clip")
        r = client.get("/api/sources", headers=_auth())
        ids = [s["source_id"] for s in r.json()]
        assert "youtube:pend_with_clip" in ids


class TestSourcesShape:
    def test_clip_count_correct(self, client):
        _insert_source("youtube:multi", status="done")
        _insert_clip("youtube:multi", hook="Hook 1")
        _insert_clip("youtube:multi", hook="Hook 2")
        r = client.get("/api/sources", headers=_auth())
        src = r.json()[0]
        assert src["clip_count"] == 2
        assert len(src["clips"]) == 2

    def test_clips_list_fields(self, client):
        _insert_source("youtube:fields", status="done")
        cid = _insert_clip("youtube:fields", hook="Great hook", gate_status="ready")
        r = client.get("/api/sources", headers=_auth())
        clip = r.json()[0]["clips"][0]
        assert clip["id"] == str(cid)
        assert clip["hook"] == "Great hook"
        assert clip["gate_status"] == "ready"
        assert "status" in clip

    def test_used_ranges_count(self, client):
        _insert_source("youtube:ranges", status="done", used_ranges=[[0, 30], [60, 90]])
        r = client.get("/api/sources", headers=_auth())
        assert r.json()[0]["used_ranges_count"] == 2

    def test_required_fields_present(self, client):
        _insert_source("youtube:reqfields", status="done")
        src = client.get("/api/sources", headers=_auth()).json()[0]
        for field in ("id", "source_id", "platform", "url", "title", "author_handle",
                      "campaign", "status", "processed_at", "clip_count", "clips",
                      "used_ranges_count", "thumbnail_url"):
            assert field in src, f"Missing field: {field}"


class TestSourcesSortOrder:
    def test_newest_first_by_processed_at(self, client):
        now = datetime.now(timezone.utc)
        _insert_source("youtube:old", status="done", processed_at=now - timedelta(hours=5))
        _insert_source("youtube:new", status="done", processed_at=now - timedelta(hours=1))
        data = client.get("/api/sources", headers=_auth()).json()
        assert data[0]["source_id"] == "youtube:new"
        assert data[1]["source_id"] == "youtube:old"


class TestSourcesCampaignFilter:
    def test_campaign_filter(self, client):
        _insert_source("youtube:fit1", campaign="fitness",  status="done")
        _insert_source("youtube:pep1", campaign="peptides", status="done")
        r = client.get("/api/sources?campaign=fitness", headers=_auth())
        data = r.json()
        assert all(s["campaign"] == "fitness" for s in data)
        assert len(data) == 1

    def test_campaign_filter_no_match(self, client):
        _insert_source("youtube:fit1", campaign="fitness", status="done")
        r = client.get("/api/sources?campaign=peptides", headers=_auth())
        assert r.json() == []


class TestSourcesSearch:
    def test_q_search_by_title(self, client):
        _insert_source("youtube:huberman", status="done", title="Dr Andrew Huberman Podcast")
        _insert_source("youtube:jre", status="done", title="Joe Rogan Experience")
        r = client.get("/api/sources?q=huberman", headers=_auth())
        data = r.json()
        assert len(data) == 1
        assert "Huberman" in data[0]["title"]

    def test_q_search_by_author_handle(self, client):
        _insert_source("youtube:ch1", status="done", author_handle="viciresearch")
        _insert_source("youtube:ch2", status="done", author_handle="someone_else")
        r = client.get("/api/sources?q=vici", headers=_auth())
        data = r.json()
        assert len(data) == 1
        assert data[0]["author_handle"] == "viciresearch"

    def test_q_no_match_returns_empty(self, client):
        _insert_source("youtube:nope", status="done", title="Fitness stuff")
        r = client.get("/api/sources?q=peptides", headers=_auth())
        assert r.json() == []


class TestSourcesThumbnail:
    def test_youtube_thumbnail_url_derived(self, client):
        _insert_source("youtube:abc123XY", status="done", platform="youtube")
        data = client.get("/api/sources", headers=_auth()).json()
        assert data[0]["thumbnail_url"] == "https://i.ytimg.com/vi/abc123XY/hqdefault.jpg"

    def test_tiktok_thumbnail_from_videoMeta(self, client):
        meta = {"videoMeta": {"coverUrl": "https://cdn.tiktok.com/cover.jpg", "width": 576, "height": 1024}}
        _insert_source(
            "tiktok:tk999",
            status="done",
            platform="tiktok",
            source_metadata=meta,
        )
        data = client.get("/api/sources", headers=_auth()).json()
        src = next(s for s in data if s["source_id"] == "tiktok:tk999")
        assert src["thumbnail_url"] == "https://cdn.tiktok.com/cover.jpg"

    def test_thumbnail_null_when_unavailable(self, client):
        _insert_source("tiktok:nothumbnail", status="done", platform="tiktok", source_metadata={})
        data = client.get("/api/sources", headers=_auth()).json()
        src = next(s for s in data if s["source_id"] == "tiktok:nothumbnail")
        assert src["thumbnail_url"] is None
