"""Regression tests for core.db.ensure_campaign.

sources.campaign / clips.campaign carry a ForeignKey to campaigns.name, but
YAML-defined campaigns never got a DB row — on Postgres (which enforces FKs,
unlike default SQLite) every source insert died with a ForeignKeyViolation and
the producer run aborted after discovery. These tests run SQLite with
PRAGMA foreign_keys=ON so the failure mode is reproducible in CI.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from core.db import ensure_campaign
from core.models import Base, Campaign, Source


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # enforce FKs like Postgres does
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionLocal()
    yield s
    s.close()


def _source(campaign: str) -> Source:
    return Source(
        source_id="youtube:abc123",
        campaign=campaign,
        platform="youtube",
        url="https://youtu.be/abc123",
    )


def test_source_insert_without_campaign_row_violates_fk(session):
    """The original production failure: no campaigns row → FK violation."""
    session.add(_source("fitness"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_ensure_campaign_seeds_row_and_unblocks_inserts(session):
    ensure_campaign(session, "fitness", enabled=True, config_snapshot={"mode": "demo"})
    row = session.query(Campaign).filter_by(name="fitness").one()
    assert row.enabled is True
    assert row.config_snapshot == {"mode": "demo"}

    session.add(_source("fitness"))
    session.commit()  # must not raise now


def test_ensure_campaign_is_idempotent_and_updates(session):
    ensure_campaign(session, "fitness")
    ensure_campaign(session, "fitness", enabled=False, config_snapshot={"v": 2})
    rows = session.query(Campaign).filter_by(name="fitness").all()
    assert len(rows) == 1
    assert rows[0].enabled is False
    assert rows[0].config_snapshot == {"v": 2}


def test_ensure_campaign_keeps_snapshot_when_none_passed(session):
    ensure_campaign(session, "fitness", config_snapshot={"keep": "me"})
    ensure_campaign(session, "fitness")  # no snapshot → previous one retained
    row = session.query(Campaign).filter_by(name="fitness").one()
    assert row.config_snapshot == {"keep": "me"}
