"""
tests/test_gate_api.py — API-level tests for the review gate endpoints.

Covers:
  - GET /api/clips returns gate_status, gate_reasons, formula_score in payload
  - POST /api/clips/{id}/override-gate sets gate_status='overridden'
  - POST /api/clips/{id}/override-gate returns 404 for unknown clip
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Test infrastructure — SQLite in-memory DB + FastAPI TestClient
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Return a TestClient backed by a file-based SQLite database.

    We use a file-based path (not :memory:) so that the engine created by
    core.db._get_engine() and any direct engines share the same on-disk store.
    """
    db_file = tmp_path / "test_gate.db"
    db_url = f"sqlite:///{db_file}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))

    # Reset settings cache so it picks up the patched env vars
    from core.settings import get_settings
    get_settings.cache_clear()

    # Reset core.db module-level engine so it recreates against the test DB
    import core.db as _db
    _db._engine = None
    _db._SessionLocal = None

    # Create the schema on the test DB via a standalone engine
    from sqlalchemy import create_engine
    from core.models import Base
    _setup_engine = create_engine(db_url)
    Base.metadata.create_all(_setup_engine)
    _setup_engine.dispose()

    # Seed a campaigns row (clips.campaign FK requires it)
    from core.db import get_session
    from core.models import Campaign
    with get_session() as session:
        session.add(Campaign(name="fitness"))
        session.commit()

    from web.api import app
    # https base_url so the Secure cookie is accepted by the test client
    c = TestClient(app, base_url="https://testserver")

    # Authenticate to get a session cookie for subsequent requests
    r = c.post("/api/auth/session", headers={"Authorization": "Bearer testpass"})
    assert r.status_code == 200

    yield c

    # Teardown: reset cached engine so it doesn't leak into other tests
    get_settings.cache_clear()
    _db._engine = None
    _db._SessionLocal = None


def _auth_headers():
    return {"Authorization": "Bearer testpass"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_clip(gate_status="pending", gate_reasons=None, formula_score=None):
    """Insert a test clip into the DB and return its id."""
    from core.db import get_session
    from core.models import Clip

    with get_session() as session:
        clip = Clip(
            campaign="fitness",
            kind="clip",
            mode="demo",
            aspect="9:16",
            status="pending_review",
            hook="Test hook",
            gate_status=gate_status,
            gate_reasons=gate_reasons,
            formula_score=formula_score,
        )
        session.add(clip)
        session.commit()
        return clip.id


# ---------------------------------------------------------------------------
# Tests: GET /api/clips returns gate fields
# ---------------------------------------------------------------------------

class TestClipPayloadGateFields:
    def test_gate_status_in_payload(self, client):
        clip_id = _insert_clip(gate_status="ready")
        r = client.get("/api/clips", headers=_auth_headers())
        assert r.status_code == 200
        clips = r.json()
        assert len(clips) == 1
        assert "gate_status" in clips[0]
        assert clips[0]["gate_status"] == "ready"

    def test_gate_reasons_in_payload(self, client):
        reasons = [{"phase": "1", "check": "resolution", "pass": True, "reason": "OK"}]
        _insert_clip(gate_status="ready", gate_reasons=reasons)
        r = client.get("/api/clips", headers=_auth_headers())
        assert r.status_code == 200
        clips = r.json()
        assert clips[0]["gate_reasons"] == reasons

    def test_formula_score_in_payload(self, client):
        _insert_clip(gate_status="ready", formula_score=0.82)
        r = client.get("/api/clips", headers=_auth_headers())
        assert r.status_code == 200
        clips = r.json()
        assert abs(clips[0]["formula_score"] - 0.82) < 1e-5

    def test_didnt_pass_clip_payload(self, client):
        fail_reasons = [
            {"phase": "1", "check": "animation_detected", "pass": False,
             "reason": "Footage appears to be animation/cartoon/CGI — auto-fail"}
        ]
        _insert_clip(gate_status="didnt_pass", gate_reasons=fail_reasons)
        r = client.get("/api/clips", headers=_auth_headers())
        assert r.status_code == 200
        clips = r.json()
        assert clips[0]["gate_status"] == "didnt_pass"
        assert clips[0]["gate_reasons"][0]["check"] == "animation_detected"

    def test_pending_gate_status_in_payload(self, client):
        """Clips with gate_status='pending' (gate hasn't run) are also returned."""
        _insert_clip(gate_status="pending")
        r = client.get("/api/clips", headers=_auth_headers())
        assert r.status_code == 200
        clips = r.json()
        assert clips[0]["gate_status"] == "pending"


# ---------------------------------------------------------------------------
# Tests: POST /api/clips/{id}/override-gate
# ---------------------------------------------------------------------------

class TestOverrideGateEndpoint:
    def test_override_sets_gate_status_overridden(self, client):
        clip_id = _insert_clip(gate_status="didnt_pass")
        r = client.post(
            f"/api/clips/{clip_id}/override-gate",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["gate_status"] == "overridden"
        assert str(data["id"]) == str(clip_id)

    def test_override_is_persisted_in_db(self, client):
        clip_id = _insert_clip(gate_status="didnt_pass")
        client.post(f"/api/clips/{clip_id}/override-gate", headers=_auth_headers())

        # Re-fetch from API and confirm persisted
        r = client.get("/api/clips", headers=_auth_headers())
        clips = r.json()
        assert clips[0]["gate_status"] == "overridden"

    def test_override_unknown_clip_returns_404(self, client):
        r = client.post("/api/clips/99999/override-gate", headers=_auth_headers())
        assert r.status_code == 404

    def test_override_requires_auth(self, client):
        clip_id = _insert_clip(gate_status="didnt_pass")
        # The `client` fixture carries a valid session cookie; clear the entire
        # cookie jar so the request is truly unauthenticated (no header, no cookie).
        client.cookies.clear()
        r = client.post(f"/api/clips/{clip_id}/override-gate")  # no auth, no cookie
        assert r.status_code == 401

    def test_override_gate_reasons_preserved(self, client):
        """Override must preserve gate_reasons so operator can still see why it failed."""
        reasons = [{"phase": "1", "check": "animation_detected", "pass": False, "reason": "auto-fail"}]
        clip_id = _insert_clip(gate_status="didnt_pass", gate_reasons=reasons)

        client.post(f"/api/clips/{clip_id}/override-gate", headers=_auth_headers())

        # gate_reasons must still be there after override
        r = client.get("/api/clips", headers=_auth_headers())
        clips = r.json()
        assert clips[0]["gate_reasons"] == reasons
        assert clips[0]["gate_status"] == "overridden"
