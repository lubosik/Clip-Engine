"""
tests/test_preferences.py — unit tests for core/preferences.py

Covers:
  - record_feedback shape
  - build_preference_context: empty, with data, char cap, safety sentence present
  - build_profile: mocked LLM creates version 1 then 2; failure returns None
  - maybe_rebuild_profile: threshold gate (< 10 = no thread, >= 10 = thread)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# SQLite fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    db_file = tmp_path / "test_prefs.db"
    db_url = f"sqlite:///{db_file}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_API_KEY", "sk-test-key")
    monkeypatch.setenv("LLM_MODEL", "claude-test")

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

    with get_session() as s:
        yield s

    get_settings.cache_clear()
    _db._engine = None
    _db._SessionLocal = None


def _insert_clip(session, source_id=None, hook="Test hook", status="pending_review",
                 review_feedback=None, score=0.8, formula_score=0.75,
                 profile_version=None, campaign="fitness"):
    from core.models import Clip
    clip = Clip(
        campaign=campaign,
        source_id=source_id,
        kind="clip",
        mode="demo",
        aspect="9:16",
        status=status,
        hook=hook,
        score=score,
        formula_score=formula_score,
        review_feedback=review_feedback,
        profile_version=profile_version,
    )
    session.add(clip)
    session.commit()
    return clip


# ---------------------------------------------------------------------------
# record_feedback tests
# ---------------------------------------------------------------------------

class TestRecordFeedback:
    def test_approved_shape(self, db_session):
        from core.preferences import record_feedback

        clip = _insert_clip(db_session)
        record_feedback(db_session, clip, "approved", [], None)
        db_session.commit()

        db_session.expire(clip)
        assert clip.review_feedback is not None
        fb = clip.review_feedback
        assert fb["action"] == "approved"
        assert fb["reasons"] == []
        assert fb["note"] is None
        assert "decided_at" in fb

    def test_rejected_shape(self, db_session):
        from core.preferences import record_feedback

        clip = _insert_clip(db_session)
        record_feedback(db_session, clip, "rejected", ["weak_hook", "boring"], "too slow")
        db_session.commit()

        db_session.expire(clip)
        fb = clip.review_feedback
        assert fb["action"] == "rejected"
        assert fb["reasons"] == ["weak_hook", "boring"]
        assert fb["note"] == "too slow"
        assert "decided_at" in fb

    def test_decided_at_is_iso_string(self, db_session):
        from core.preferences import record_feedback

        clip = _insert_clip(db_session)
        record_feedback(db_session, clip, "approved", [], None)
        fb = clip.review_feedback
        # Should be parseable as ISO datetime
        datetime.fromisoformat(fb["decided_at"])


# ---------------------------------------------------------------------------
# build_preference_context tests
# ---------------------------------------------------------------------------

class TestBuildPreferenceContext:
    def test_returns_empty_when_no_data(self, db_session):
        from core.preferences import build_preference_context
        result = build_preference_context(db_session, "fitness")
        assert result == ""

    def test_returns_empty_when_no_profile_and_no_clips(self, db_session):
        from core.preferences import build_preference_context
        result = build_preference_context(db_session, "nonexistent_campaign")
        assert result == ""

    def test_includes_safety_guard_sentence(self, db_session):
        from core.preferences import build_preference_context, SAFETY_GUARD_SENTENCE

        # Insert a decided clip to produce non-empty context
        _insert_clip(
            db_session,
            hook="Great content",
            review_feedback={
                "action": "approved",
                "reasons": [],
                "note": None,
                "decided_at": datetime.now(tz=timezone.utc).isoformat(),
            },
        )

        result = build_preference_context(db_session, "fitness")
        assert result != ""
        assert SAFETY_GUARD_SENTENCE in result

    def test_includes_approved_and_rejected_examples(self, db_session):
        from core.preferences import build_preference_context, record_feedback

        c1 = _insert_clip(db_session, hook="Hook approved", source_id="yt:001")
        record_feedback(db_session, c1, "approved", [], None)
        db_session.commit()

        c2 = _insert_clip(db_session, hook="Hook rejected", source_id="yt:001")
        record_feedback(db_session, c2, "rejected", ["weak_hook"], "too short")
        db_session.commit()

        result = build_preference_context(db_session, "fitness")
        assert "APPROVED" in result
        assert "REJECTED" in result
        assert "weak_hook" in result

    def test_capped_at_1800_chars(self, db_session):
        from core.preferences import build_preference_context, record_feedback

        # Insert many clips with long hooks
        for i in range(20):
            c = _insert_clip(
                db_session,
                hook="x" * 100 + f" clip {i}",
                source_id=f"yt:{i}",
            )
            record_feedback(db_session, c, "approved" if i % 2 == 0 else "rejected",
                            ["boring"] if i % 2 else [], None)
            db_session.commit()

        result = build_preference_context(db_session, "fitness")
        assert len(result) <= 1800

    def test_includes_profile_rules_when_present(self, db_session):
        from core.models import PreferenceProfile
        from core.preferences import build_preference_context, record_feedback

        profile = PreferenceProfile(
            campaign="fitness",
            version=1,
            rules=["prefer hooks under 15 words", "reject clips with no clear ending"],
            meta={},
        )
        db_session.add(profile)
        db_session.commit()

        # Need at least one decided clip for non-empty context
        c = _insert_clip(db_session, hook="Some hook")
        record_feedback(db_session, c, "approved", [], None)
        db_session.commit()

        result = build_preference_context(db_session, "fitness")
        assert "prefer hooks under 15 words" in result
        assert "PREFERENCE PROFILE" in result

    def test_prefers_contrasting_pairs_from_same_source(self, db_session):
        from core.preferences import build_preference_context, record_feedback

        # Two clips from the same source — one approved, one rejected
        c1 = _insert_clip(db_session, hook="Approved from same source", source_id="yt:pair1")
        record_feedback(db_session, c1, "approved", [], None)
        db_session.commit()

        c2 = _insert_clip(db_session, hook="Rejected from same source", source_id="yt:pair1")
        record_feedback(db_session, c2, "rejected", ["boring"], None)
        db_session.commit()

        result = build_preference_context(db_session, "fitness")
        # Both should appear since they're a contrasting pair
        assert "Approved from same source" in result
        assert "Rejected from same source" in result


# ---------------------------------------------------------------------------
# build_profile tests
# ---------------------------------------------------------------------------

class TestBuildProfile:
    def _mock_llm_response(self, rules_list):
        """Return a mock Anthropic message response with the given rules."""
        mock_message = MagicMock()
        mock_message.content = [MagicMock(type="text", text=json.dumps(rules_list))]
        return mock_message

    def test_creates_version_1_on_first_build(self, db_session):
        from core.models import PreferenceProfile
        from core.preferences import build_profile, record_feedback

        # Insert enough decided clips
        for i in range(6):
            c = _insert_clip(db_session, hook=f"Hook {i}")
            record_feedback(
                db_session, c,
                "approved" if i % 2 == 0 else "rejected",
                ["boring"] if i % 2 else [],
                None,
            )
            db_session.commit()

        rules = ["prefer hooks under 15 words", "reject clips with no clear ending"]

        with patch("core.llm.create_completion") as mock_create, \
             patch("core.llm.extract_text", return_value=json.dumps(rules)):
            mock_create.return_value = MagicMock()
            with patch("anthropic.Anthropic"):
                profile = build_profile(db_session, "fitness", min_decisions=1)

        assert profile is not None
        assert profile.version == 1
        assert profile.rules == rules
        assert profile.campaign == "fitness"

    def test_creates_version_2_on_second_build(self, db_session):
        from core.models import PreferenceProfile
        from core.preferences import build_profile, record_feedback

        # Insert decisions
        for i in range(6):
            c = _insert_clip(db_session, hook=f"Hook {i}")
            record_feedback(db_session, c, "approved", [], None)
            db_session.commit()

        rules_v2 = ["new rule A", "new rule B"]

        with patch("core.llm.create_completion") as mock_create, \
             patch("core.llm.extract_text", return_value=json.dumps(rules_v2)):
            mock_create.return_value = MagicMock()
            with patch("anthropic.Anthropic"):
                # First build
                p1 = build_profile(db_session, "fitness", min_decisions=1)
                # Second build
                p2 = build_profile(db_session, "fitness", min_decisions=1)

        assert p1 is not None
        assert p2 is not None
        assert p1.version == 1
        assert p2.version == 2

    def test_returns_none_when_llm_fails(self, db_session):
        from core.preferences import build_profile, record_feedback

        for i in range(6):
            c = _insert_clip(db_session, hook=f"Hook {i}")
            record_feedback(db_session, c, "approved", [], None)
            db_session.commit()

        with patch("core.llm.create_completion", side_effect=RuntimeError("LLM error")):
            with patch("anthropic.Anthropic"):
                profile = build_profile(db_session, "fitness", min_decisions=1)

        assert profile is None

    def test_returns_none_below_min_decisions(self, db_session):
        from core.preferences import build_profile

        # Only 2 decided clips, min=5
        for i in range(2):
            c = _insert_clip(db_session, hook=f"Hook {i}",
                             review_feedback={"action": "approved", "reasons": [],
                                              "note": None, "decided_at": "2026-01-01T00:00:00+00:00"})
            db_session.commit()

        profile = build_profile(db_session, "fitness", min_decisions=5)
        assert profile is None

    def test_bad_llm_json_returns_empty_rules(self, db_session):
        from core.preferences import build_profile, record_feedback

        for i in range(6):
            c = _insert_clip(db_session, hook=f"Hook {i}")
            record_feedback(db_session, c, "approved", [], None)
            db_session.commit()

        with patch("core.llm.create_completion") as mock_create, \
             patch("core.llm.extract_text", return_value="not valid json at all"):
            mock_create.return_value = MagicMock()
            with patch("anthropic.Anthropic"):
                profile = build_profile(db_session, "fitness", min_decisions=1)

        # Should still create a profile but with empty rules
        assert profile is not None
        assert profile.rules == []

    def test_meta_populated(self, db_session):
        from core.preferences import build_profile, record_feedback

        for i in range(4):
            c = _insert_clip(db_session, hook=f"Hook {i}")
            record_feedback(db_session, c, "approved" if i % 2 == 0 else "rejected",
                            ["boring"] if i % 2 else [], None)
            db_session.commit()

        with patch("core.llm.create_completion") as mock_create, \
             patch("core.llm.extract_text", return_value='["rule1"]'):
            mock_create.return_value = MagicMock()
            with patch("anthropic.Anthropic"):
                profile = build_profile(db_session, "fitness", min_decisions=1)

        assert profile is not None
        assert profile.meta is not None
        assert "decisions_count" in profile.meta
        assert profile.meta["decisions_count"] >= 1


# ---------------------------------------------------------------------------
# maybe_rebuild_profile tests
# ---------------------------------------------------------------------------

class TestMaybeRebuildProfile:
    def test_no_rebuild_below_threshold(self, db_session):
        from core.preferences import maybe_rebuild_profile, record_feedback

        # Insert 9 decisions (below threshold of 10)
        for i in range(9):
            c = _insert_clip(db_session, hook=f"Hook {i}")
            record_feedback(db_session, c, "approved", [], None)
            db_session.commit()

        with patch("core.preferences.build_profile") as mock_build:
            # Run from a fresh session since maybe_rebuild spawns a thread with new session
            from core.db import get_session as _gs
            with _gs() as fresh:
                maybe_rebuild_profile(fresh, "fitness")
            # Give the thread a moment — but since threshold not met, no thread spawned
            import time
            time.sleep(0.1)
            # build_profile should NOT have been called
            mock_build.assert_not_called()

    def test_rebuild_at_threshold(self, db_session):
        from core.preferences import maybe_rebuild_profile, record_feedback

        # Insert exactly 10 decisions
        for i in range(10):
            c = _insert_clip(db_session, hook=f"Hook {i}")
            record_feedback(db_session, c, "approved", [], None)
            db_session.commit()

        build_called = []

        def _fake_build(session, campaign, **kwargs):
            build_called.append(campaign)
            return None  # doesn't need to return a real profile

        with patch("core.preferences.build_profile", side_effect=_fake_build):
            from core.db import get_session as _gs
            with _gs() as fresh:
                maybe_rebuild_profile(fresh, "fitness")
            # Wait for daemon thread
            import time
            time.sleep(0.3)

        assert "fitness" in build_called

    def test_never_raises(self, db_session):
        from core.preferences import maybe_rebuild_profile

        # Should not raise even with broken DB (session returning None)
        bad_session = MagicMock()
        bad_session.query.side_effect = RuntimeError("DB is down")

        # Must not raise
        maybe_rebuild_profile(bad_session, "fitness")
