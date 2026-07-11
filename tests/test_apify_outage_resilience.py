"""Regression tests for Apify-outage resilience (2026-07-11).

Locks in the fixes for the run that died with 0 clips while Apify was at its
monthly usage hard limit:
  1. A transcript ACTOR failure raises TranscriptFetchError (retryable) instead
     of returning [] — so the source is NOT wrongly marked done.
  2. _load_backlog_sources lets run_campaign proceed from unfinished DB sources
     when discovery yields nothing, preferring transcript-cached sources.
"""

from __future__ import annotations

import pytest

from producer.transcripts import (
    TranscriptFetchError,
    fetch_youtube_transcript,
    fetch_tiktok_transcript,
)
from producer.run import _extract_view_count, _load_backlog_sources


class _BrokenApify:
    """Simulates the actor-run failure seen at the monthly usage hard limit."""

    def run(self, actor_id, run_input, **kw):
        raise RuntimeError("Monthly usage hard limit exceeded")


class _EmptyApify:
    """Actor run succeeds but the video genuinely has no transcript."""

    def run(self, actor_id, run_input, **kw):
        return []


# ---------------------------------------------------------------------------
# TranscriptFetchError
# ---------------------------------------------------------------------------

def test_youtube_actor_failure_raises_retryable_error():
    with pytest.raises(TranscriptFetchError):
        fetch_youtube_transcript("https://youtu.be/abc", _BrokenApify())


def test_tiktok_actor_failure_raises_retryable_error():
    with pytest.raises(TranscriptFetchError):
        fetch_tiktok_transcript("https://tiktok.com/@x/video/1", _BrokenApify())


def test_youtube_genuinely_missing_transcript_still_returns_empty():
    # Actor ran fine, no transcript exists → [] (source may be marked done).
    assert fetch_youtube_transcript("https://youtu.be/abc", _EmptyApify()) == []


def test_process_source_leaves_status_on_transcript_fetch_error(tmp_path, monkeypatch):
    """A transient transcript failure must not mark the source done."""
    import core.db as db_mod
    from core.db import get_session, ensure_campaign
    from core.models import Source
    from producer.run import _process_source
    from core.config import load_campaign
    from pathlib import Path

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    from core.settings import get_settings
    get_settings.cache_clear()
    db_mod._engine = None
    db_mod._SessionLocal = None
    try:
        from core.models import Base
        from core.db import _get_engine
        Base.metadata.create_all(_get_engine())

        cfg = load_campaign(Path("campaigns") / "fitness.yaml", strict_assets=False)

        with get_session() as session:
            ensure_campaign(session, cfg.name, enabled=True, config_snapshot=None)
            session.add(Source(
                source_id="youtube:outage1",
                campaign=cfg.name,
                platform="youtube",
                url="https://youtu.be/outage1",
                status="pending",
                used_ranges=[],
            ))
            session.commit()

            candidate = {
                "source_id": "youtube:outage1",
                "platform": "youtube",
                "url": "https://youtu.be/outage1",
            }
            clips = _process_source(
                candidate, cfg, _BrokenApify(), session, 2, run_mode="demo"
            )
            assert clips == []

        with get_session() as session:
            row = session.query(Source).filter_by(source_id="youtube:outage1").first()
            assert row.status == "pending"  # NOT 'done' — retry on a future run
    finally:
        get_settings.cache_clear()
        db_mod._engine = None
        db_mod._SessionLocal = None


# ---------------------------------------------------------------------------
# Backlog fallback
# ---------------------------------------------------------------------------

def test_extract_view_count_platform_keys():
    assert _extract_view_count({"viewCount": 123}) == 123
    assert _extract_view_count({"playCount": "456"}) == 456
    assert _extract_view_count({"view_count": 7.0}) == 7
    assert _extract_view_count({"unrelated": 1}) == 0
    assert _extract_view_count(None) == 0
    assert _extract_view_count({"viewCount": True}) == 0


def test_load_backlog_sources_prefers_transcribed_then_views(tmp_path, monkeypatch):
    import core.db as db_mod
    from core.db import get_session, ensure_campaign
    from core.models import Source, Transcript

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/b.db")
    from core.settings import get_settings
    get_settings.cache_clear()
    db_mod._engine = None
    db_mod._SessionLocal = None
    try:
        from core.models import Base
        from core.db import _get_engine
        Base.metadata.create_all(_get_engine())

        with get_session() as session:
            ensure_campaign(session, "camp", enabled=True, config_snapshot=None)
            rows = [
                # (source_id, status, views, has_transcript)
                ("youtube:done1", "done", 9_000_000, True),          # excluded: done
                ("youtube:pend_hi", "pending", 500_000, False),      # no transcript
                ("youtube:pend_lo", "pending", 100, True),           # transcribed
                ("youtube:sel", "selected", 40_000, True),           # transcribed
                ("youtube:part", "partially_done", 90_000, False),   # no transcript
            ]
            for sid, status, views, has_t in rows:
                session.add(Source(
                    source_id=sid, campaign="camp", platform="youtube",
                    url=f"https://youtu.be/{sid.split(':')[1]}",
                    status=status, used_ranges=[],
                    source_metadata={"viewCount": views},
                ))
                if has_t:
                    session.add(Transcript(source_id=sid, segments=[
                        {"start": 0.0, "end": 1.0, "text": "hi"}
                    ]))
            session.commit()

            backlog = _load_backlog_sources(session, "camp")

        ids = [c["source_id"] for c in backlog]
        # done excluded; transcribed first (by views), then untranscribed (by views)
        assert ids == ["youtube:sel", "youtube:pend_lo", "youtube:pend_hi", "youtube:part"]
        first = backlog[0]
        assert first["platform"] == "youtube"
        assert first["view_count"] == 40_000
        assert first["url"].startswith("https://youtu.be/")
        assert isinstance(first["raw"], dict)
    finally:
        get_settings.cache_clear()
        db_mod._engine = None
        db_mod._SessionLocal = None
