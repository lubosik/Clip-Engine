"""
tests/test_sources_progress_api.py — API-level tests for pipeline progress features.

Covers:
  - GET /api/sources: new stage/clip-count fields present on each row
  - GET /api/sources?in_progress=1: correct in-progress filter
  - GET /api/sources: clips_approved/rejected/pending counts correct
  - GET /api/analytics/approval-rate: shape + enough_data=false for < 10 decisions
  - POST /api/clips/{id}/reject: validates codes, legacy body accepted, review_feedback written
  - POST /api/clips/{id}/approve: review_feedback written
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixture (mirrors test_gate_api.py pattern)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_progress.db"
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
    from core.models import Campaign
    with get_session() as s:
        s.add(Campaign(name="fitness"))
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


def _insert_source(
    source_id: str,
    *,
    campaign: str = "fitness",
    platform: str = "youtube",
    status: str = "done",
    stage: str = "reviewing",
    stage_updated_at=None,
    stage_error: str | None = None,
    clips_identified: int | None = None,
) -> None:
    from core.db import get_session
    from core.models import Source
    with get_session() as s:
        src = Source(
            source_id=source_id,
            campaign=campaign,
            platform=platform,
            url=f"https://example.com/{source_id}",
            status=status,
            stage=stage,
            stage_updated_at=stage_updated_at or datetime.now(tz=timezone.utc),
            stage_error=stage_error,
            clips_identified=clips_identified,
        )
        s.add(src)
        s.commit()


def _insert_clip(
    source_id: str,
    *,
    campaign: str = "fitness",
    hook: str = "Test hook",
    status: str = "pending_review",
    review_feedback=None,
    profile_version: int | None = None,
) -> int:
    from core.db import get_session
    from core.models import Clip
    with get_session() as s:
        clip = Clip(
            campaign=campaign,
            source_id=source_id,
            kind="clip",
            mode="demo",
            aspect="9:16",
            status=status,
            hook=hook,
            review_feedback=review_feedback,
            profile_version=profile_version,
        )
        s.add(clip)
        s.commit()
        return clip.id


# ---------------------------------------------------------------------------
# Stage / clip-count fields on GET /api/sources
# ---------------------------------------------------------------------------

class TestSourcesExtendedFields:
    def test_stage_field_present(self, client):
        _insert_source("youtube:ext001", status="done", stage="complete")
        data = client.get("/api/sources", headers=_auth()).json()
        assert len(data) == 1
        src = data[0]
        assert "stage" in src
        assert "clips_identified" in src
        assert "stage_error" in src
        assert "stage_updated_at" in src
        assert "clips_rendered" in src
        assert "clips_approved" in src
        assert "clips_rejected" in src
        assert "clips_pending" in src
        assert "exhaustion" in src

    def test_exhaustion_fully_exhausted(self, client):
        _insert_source("youtube:done001", status="done", stage="complete")
        data = client.get("/api/sources", headers=_auth()).json()
        assert data[0]["exhaustion"] == "fully_exhausted"

    def test_exhaustion_partially_used(self, client):
        _insert_source("youtube:partial001", status="partially_done", stage="reviewing")
        data = client.get("/api/sources", headers=_auth()).json()
        assert data[0]["exhaustion"] == "partially_used"

    def test_exhaustion_in_progress_for_pending(self, client):
        # pending status with a clip so it shows up
        _insert_source("youtube:pend001", status="pending", stage="transcribing")
        _insert_clip("youtube:pend001")
        data = client.get("/api/sources", headers=_auth()).json()
        src = next(s for s in data if s["source_id"] == "youtube:pend001")
        assert src["exhaustion"] == "in_progress"

    def test_clip_counts_correct(self, client):
        _insert_source("youtube:counts001", status="done", stage="reviewing")
        _insert_clip("youtube:counts001", status="pending_review")
        _insert_clip("youtube:counts001", status="approved")
        _insert_clip("youtube:counts001", status="rejected")

        data = client.get("/api/sources", headers=_auth()).json()
        src = next(s for s in data if s["source_id"] == "youtube:counts001")

        assert src["clips_rendered"] == 3
        assert src["clips_approved"] == 1
        assert src["clips_rejected"] == 1
        assert src["clips_pending"] == 1

    def test_stage_complete_derived_from_reviewing_plus_no_pending(self, client):
        """stage='reviewing' + all clips decided → derived stage='complete'."""
        _insert_source("youtube:derived001", status="done", stage="reviewing")
        _insert_clip("youtube:derived001", status="approved")

        data = client.get("/api/sources", headers=_auth()).json()
        src = next(s for s in data if s["source_id"] == "youtube:derived001")
        # All clips decided (approved), so stage should be derived as 'complete'
        assert src["stage"] == "complete"

    def test_clips_identified_passed_through(self, client):
        _insert_source(
            "youtube:ident001",
            status="done",
            stage="complete",
            clips_identified=7,
        )
        data = client.get("/api/sources", headers=_auth()).json()
        src = next(s for s in data if s["source_id"] == "youtube:ident001")
        assert src["clips_identified"] == 7

    def test_stage_error_passed_through(self, client):
        _insert_source(
            "youtube:err001",
            status="done",  # done so it shows in default view
            stage="failed",
            stage_error="DRM protected video",
        )
        data = client.get("/api/sources", headers=_auth()).json()
        src = next(s for s in data if s["source_id"] == "youtube:err001")
        assert src["stage_error"] == "DRM protected video"


# ---------------------------------------------------------------------------
# ?in_progress=1 filter
# ---------------------------------------------------------------------------

class TestSourcesInProgressFilter:
    def test_transcribing_source_included(self, client):
        _insert_source("youtube:inprog001", status="pending", stage="transcribing")
        data = client.get("/api/sources?in_progress=1", headers=_auth()).json()
        ids = [s["source_id"] for s in data]
        assert "youtube:inprog001" in ids

    def test_identifying_source_included(self, client):
        _insert_source("youtube:inprog002", status="pending", stage="identifying")
        data = client.get("/api/sources?in_progress=1", headers=_auth()).json()
        ids = [s["source_id"] for s in data]
        assert "youtube:inprog002" in ids

    def test_complete_source_excluded(self, client):
        _insert_source("youtube:done001", status="done", stage="complete")
        data = client.get("/api/sources?in_progress=1", headers=_auth()).json()
        ids = [s["source_id"] for s in data]
        assert "youtube:done001" not in ids

    def test_queued_source_excluded(self, client):
        _insert_source("youtube:queued001", status="pending", stage="queued")
        # Even with a clip it should not appear unless it has pending clips
        data = client.get("/api/sources?in_progress=1", headers=_auth()).json()
        ids = [s["source_id"] for s in data]
        assert "youtube:queued001" not in ids

    def test_old_failed_source_excluded(self, client):
        old_ts = datetime.now(tz=timezone.utc) - timedelta(hours=25)
        _insert_source(
            "youtube:oldfail001",
            status="done",
            stage="failed",
            stage_updated_at=old_ts,
        )
        data = client.get("/api/sources?in_progress=1", headers=_auth()).json()
        ids = [s["source_id"] for s in data]
        assert "youtube:oldfail001" not in ids

    def test_recent_failed_source_included(self, client):
        recent_ts = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        _insert_source(
            "youtube:newfail001",
            status="pending",
            stage="failed",
            stage_updated_at=recent_ts,
        )
        data = client.get("/api/sources?in_progress=1", headers=_auth()).json()
        ids = [s["source_id"] for s in data]
        assert "youtube:newfail001" in ids

    def test_source_with_pending_clips_included_regardless_of_stage(self, client):
        """A source with pending clips must appear in in_progress regardless of stage."""
        _insert_source("youtube:pendclip001", status="done", stage="complete")
        _insert_clip("youtube:pendclip001", status="pending_review")

        data = client.get("/api/sources?in_progress=1", headers=_auth()).json()
        ids = [s["source_id"] for s in data]
        assert "youtube:pendclip001" in ids

    def test_default_still_shows_done_sources(self, client):
        """Default filter (no in_progress) still returns done sources."""
        _insert_source("youtube:done002", status="done", stage="complete")
        data = client.get("/api/sources", headers=_auth()).json()
        ids = [s["source_id"] for s in data]
        assert "youtube:done002" in ids


# ---------------------------------------------------------------------------
# Approval-rate endpoint
# ---------------------------------------------------------------------------

class TestApprovalRateEndpoint:
    def test_endpoint_shape(self, client):
        r = client.get("/api/analytics/approval-rate?campaign=fitness", headers=_auth())
        assert r.status_code == 200
        data = r.json()
        assert "campaign" in data
        assert "weeks" in data
        assert "total_decisions" in data
        assert "enough_data" in data
        assert data["campaign"] == "fitness"

    def test_enough_data_false_below_10(self, client):
        r = client.get("/api/analytics/approval-rate?campaign=fitness", headers=_auth())
        data = r.json()
        assert data["enough_data"] is False
        assert data["total_decisions"] == 0

    def test_enough_data_true_at_10(self, client):
        _insert_source("youtube:rate001", status="done", stage="complete")
        for i in range(10):
            decided_at = datetime.now(tz=timezone.utc).isoformat()
            _insert_clip(
                "youtube:rate001",
                status="approved" if i % 2 == 0 else "rejected",
                review_feedback={
                    "action": "approved" if i % 2 == 0 else "rejected",
                    "reasons": [] if i % 2 == 0 else ["boring"],
                    "note": None,
                    "decided_at": decided_at,
                },
            )

        r = client.get("/api/analytics/approval-rate?campaign=fitness", headers=_auth())
        data = r.json()
        assert data["total_decisions"] == 10
        assert data["enough_data"] is True
        assert len(data["weeks"]) >= 1

    def test_week_shape(self, client):
        _insert_source("youtube:weekshape", status="done", stage="complete")
        decided_at = datetime.now(tz=timezone.utc).isoformat()
        _insert_clip(
            "youtube:weekshape",
            status="approved",
            review_feedback={
                "action": "approved",
                "reasons": [],
                "note": None,
                "decided_at": decided_at,
            },
            profile_version=1,
        )

        r = client.get("/api/analytics/approval-rate?campaign=fitness&weeks=4", headers=_auth())
        data = r.json()
        assert len(data["weeks"]) >= 1
        week = data["weeks"][0]
        assert "week_start" in week
        assert "approved" in week
        assert "rejected" in week
        assert "rate" in week
        assert "profile_versions" in week
        assert isinstance(week["profile_versions"], list)

    def test_campaign_required(self, client):
        r = client.get("/api/analytics/approval-rate", headers=_auth())
        # campaign is required — should return 422
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Reject endpoint validation
# ---------------------------------------------------------------------------

class TestRejectEndpointValidation:
    def _make_clip(self):
        from core.db import get_session
        from core.models import Source, Clip
        with get_session() as s:
            src = Source(
                source_id="youtube:rejtest",
                campaign="fitness",
                platform="youtube",
                url="https://youtube.com/watch?v=rej",
                status="done",
                stage="reviewing",
            )
            s.add(src)
            s.flush()
            clip = Clip(
                campaign="fitness",
                source_id="youtube:rejtest",
                kind="clip",
                mode="demo",
                aspect="9:16",
                status="pending_review",
                hook="Test hook",
            )
            s.add(clip)
            s.commit()
            return clip.id

    def test_valid_reasons_accepted(self, client):
        clip_id = self._make_clip()
        r = client.post(
            f"/api/clips/{clip_id}/reject",
            headers=_auth(),
            json={"reasons": ["weak_hook", "boring"]},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_unknown_code_returns_422(self, client):
        clip_id = self._make_clip()
        r = client.post(
            f"/api/clips/{clip_id}/reject",
            headers=_auth(),
            json={"reasons": ["not_a_real_code"]},
        )
        assert r.status_code == 422

    def test_empty_reasons_returns_422(self, client):
        clip_id = self._make_clip()
        r = client.post(
            f"/api/clips/{clip_id}/reject",
            headers=_auth(),
            json={"reasons": []},
        )
        assert r.status_code == 422

    def test_legacy_body_accepted(self, client):
        """Legacy {"reason": "text"} → mapped to reasons=["other"], note=text."""
        clip_id = self._make_clip()
        r = client.post(
            f"/api/clips/{clip_id}/reject",
            headers=_auth(),
            json={"reason": "This clip is too short"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

        # Check review_feedback was written
        from core.db import get_session
        from core.models import Clip
        with get_session() as s:
            clip = s.get(Clip, clip_id)
            fb = clip.review_feedback
            assert fb is not None
            assert fb["action"] == "rejected"
            assert "other" in fb["reasons"]
            assert fb["note"] == "This clip is too short"

    def test_review_feedback_written_on_reject(self, client):
        clip_id = self._make_clip()
        r = client.post(
            f"/api/clips/{clip_id}/reject",
            headers=_auth(),
            json={"reasons": ["weak_hook"], "note": "Hook is too vague"},
        )
        assert r.status_code == 200

        from core.db import get_session
        from core.models import Clip
        with get_session() as s:
            clip = s.get(Clip, clip_id)
            fb = clip.review_feedback
            assert fb is not None
            assert fb["action"] == "rejected"
            assert "weak_hook" in fb["reasons"]
            assert fb["note"] == "Hook is too vague"
            assert "decided_at" in fb

    def test_reject_reason_back_compat(self, client):
        """clip.reject_reason is populated for backward compatibility."""
        clip_id = self._make_clip()
        client.post(
            f"/api/clips/{clip_id}/reject",
            headers=_auth(),
            json={"reasons": ["weak_hook"], "note": "No tension"},
        )

        from core.db import get_session
        from core.models import Clip
        with get_session() as s:
            clip = s.get(Clip, clip_id)
            assert clip.reject_reason is not None
            # Should contain the human-readable label for weak_hook
            assert "Weak hook" in clip.reject_reason


# ---------------------------------------------------------------------------
# Approve endpoint - review_feedback written
# ---------------------------------------------------------------------------

class TestApproveEndpointFeedback:
    def _make_clip(self):
        from core.db import get_session
        from core.models import Source, Clip
        with get_session() as s:
            # Use a unique source_id per test call
            src_id = f"youtube:apptest-{id(self)}"
            try:
                src = Source(
                    source_id=src_id,
                    campaign="fitness",
                    platform="youtube",
                    url=f"https://youtube.com/watch?v={src_id}",
                    status="done",
                    stage="reviewing",
                )
                s.add(src)
                s.flush()
            except Exception:
                s.rollback()
                src_id = f"youtube:apptest-{id(self)}-2"
                src = Source(
                    source_id=src_id,
                    campaign="fitness",
                    platform="youtube",
                    url=f"https://youtube.com/watch?v={src_id}",
                    status="done",
                    stage="reviewing",
                )
                s.add(src)
                s.flush()

            clip = Clip(
                campaign="fitness",
                source_id=src_id,
                kind="clip",
                mode="demo",
                aspect="9:16",
                status="pending_review",
                hook="Approve this hook",
            )
            s.add(clip)
            s.commit()
            return clip.id

    def test_review_feedback_written_on_approve(self, client):
        clip_id = self._make_clip()
        r = client.post(
            f"/api/clips/{clip_id}/approve",
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

        from core.db import get_session
        from core.models import Clip
        with get_session() as s:
            clip = s.get(Clip, clip_id)
            fb = clip.review_feedback
            assert fb is not None
            assert fb["action"] == "approved"
            assert fb["reasons"] == []
            assert "decided_at" in fb
