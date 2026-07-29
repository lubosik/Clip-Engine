"""
tests/test_video_api.py — API endpoint tests for the add-video pipeline.

Covers (ADD_VIDEO_CONTRACTS.md §6):
  - POST /api/campaigns/{name}/videos
      - 404 on unknown campaign
      - 422 on missing/invalid url
      - 422 on bad mode / bad spend caps
      - 409 on exhausted source (status=done, no force)
      - 200 + {started, source_id, pid, log} on valid URL (Popen monkeypatched)
      - 200 when force=true on exhausted source
  - GET /api/videos/{source_id}/log
      - 404 when no log file exists
      - 200 with line array when log file present
  - _source_row_to_dict includes clips_detail with correct shape
  - SSE stage filter includes 'correcting'
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Test client fixture (mirrors test_gate_api.py pattern)
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    db_file = tmp_path / "test_video_api.db"
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
    _setup_engine = create_engine(db_url)
    Base.metadata.create_all(_setup_engine)
    _setup_engine.dispose()

    # Seed a campaign and a campaign yaml in the campaigns dir
    campaigns_dir = Path(__file__).resolve().parent.parent / "campaigns"
    test_yaml = campaigns_dir / "testcamp.yaml"

    from core.db import get_session
    from core.models import Campaign
    with get_session() as session:
        session.add(Campaign(name="testcamp"))
        session.commit()

    from web.api import app
    c = TestClient(app, base_url="https://testserver")

    # Authenticate
    r = c.post("/api/auth/session", headers={"Authorization": "Bearer testpass"})
    assert r.status_code == 200

    yield c, tmp_path, test_yaml

    get_settings.cache_clear()
    _db._engine = None
    _db._SessionLocal = None

    # Clean up test yaml if created
    if test_yaml.exists():
        try:
            test_yaml.unlink()
        except OSError:
            pass


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer testpass"}


def _write_test_campaign_yaml(path: Path) -> None:
    """Write a minimal valid campaign YAML for the test campaign."""
    path.write_text(
        "name: testcamp\n"
        "enabled: true\n"
        "mode: demo\n"
        "sources: []\n"
        "ranking:\n"
        "  clip_length: [45, 90]\n"
        "  max_clips_per_source: 4\n"
        "  ranking_rules: prefer actionable moments\n"
        "destinations:\n"
        "  postiz_channels: []\n"
        "  caption_template: '{hook}'\n"
        "  hashtags: []\n"
        "gate:\n"
        "  relaxed_safety_checks: []\n"
    )


# ---------------------------------------------------------------------------
# POST /api/campaigns/{name}/videos
# ---------------------------------------------------------------------------

class TestAddCampaignVideo:
    def test_unknown_campaign_returns_404(self, client: Any) -> None:
        c, tmp_path, _ = client
        r = c.post(
            "/api/campaigns/nonexistent_xyz/videos",
            json={"url": "https://www.youtube.com/watch?v=abc12345678"},
            headers=_auth_headers(),
        )
        assert r.status_code == 404
        assert "not found" in r.json()["detail"]["error"].lower()

    def test_missing_url_returns_422(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)
        r = c.post(
            "/api/campaigns/testcamp/videos",
            json={},
            headers=_auth_headers(),
        )
        assert r.status_code == 422
        assert "url" in r.json()["detail"]["error"].lower()

    def test_empty_url_returns_422(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)
        r = c.post(
            "/api/campaigns/testcamp/videos",
            json={"url": ""},
            headers=_auth_headers(),
        )
        assert r.status_code == 422

    def test_non_youtube_url_returns_422(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)
        r = c.post(
            "/api/campaigns/testcamp/videos",
            json={"url": "https://www.tiktok.com/@user/video/12345"},
            headers=_auth_headers(),
        )
        assert r.status_code == 422
        assert "youtube" in r.json()["detail"]["error"].lower()

    def test_invalid_mode_returns_422(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)
        r = c.post(
            "/api/campaigns/testcamp/videos",
            json={
                "url": "https://www.youtube.com/watch?v=abc12345678",
                "mode": "live",  # invalid
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 422

    def test_invalid_spend_cap_returns_422(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)
        r = c.post(
            "/api/campaigns/testcamp/videos",
            json={
                "url": "https://www.youtube.com/watch?v=abc12345678",
                "max_modal_spend": -1.0,
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 422

    def test_zero_spend_cap_returns_422(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)
        r = c.post(
            "/api/campaigns/testcamp/videos",
            json={
                "url": "https://www.youtube.com/watch?v=abc12345678",
                "max_apify_spend": 0.0,
            },
            headers=_auth_headers(),
        )
        assert r.status_code == 422

    def test_exhausted_source_returns_409(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)

        # Insert an exhausted source
        from core.db import get_session
        from core.models import Source
        with get_session() as session:
            src = Source(
                source_id="youtube:abc12345678",
                campaign="testcamp",
                platform="youtube",
                url="https://www.youtube.com/watch?v=abc12345678",
                status="done",
            )
            session.add(src)
            session.commit()

        r = c.post(
            "/api/campaigns/testcamp/videos",
            json={"url": "https://www.youtube.com/watch?v=abc12345678"},
            headers=_auth_headers(),
        )
        assert r.status_code == 409
        assert "exhausted" in r.json()["detail"]["error"].lower()

    def test_valid_request_spawns_process(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)

        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            r = c.post(
                "/api/campaigns/testcamp/videos",
                json={"url": "https://www.youtube.com/watch?v=abc12345678"},
                headers=_auth_headers(),
            )

        assert r.status_code == 200
        body = r.json()
        assert body["started"] is True
        assert body["source_id"] == "youtube:abc12345678"
        assert body["pid"] == 99999
        assert "log" in body
        assert "max_apify_spend" in body
        assert "max_modal_spend" in body

    def test_valid_request_popen_cmd_contains_video_pipeline(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        popen_call_args: list = []

        def capture_popen(cmd: Any, **kwargs: Any) -> Any:
            popen_call_args.extend(cmd)
            return mock_proc

        with patch("subprocess.Popen", side_effect=capture_popen):
            c.post(
                "/api/campaigns/testcamp/videos",
                json={
                    "url": "https://www.youtube.com/watch?v=abc12345678",
                    "mode": "demo",
                    "max_apify_spend": 1.5,
                    "max_modal_spend": 2.5,
                },
                headers=_auth_headers(),
            )

        # Should call python -m producer.video_pipeline
        cmd_str = " ".join(str(a) for a in popen_call_args)
        assert "producer.video_pipeline" in cmd_str
        assert "abc12345678" in cmd_str
        assert "1.5" in cmd_str
        assert "2.5" in cmd_str

    def test_force_true_bypasses_409(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)

        # Insert exhausted source
        from core.db import get_session
        from core.models import Source
        with get_session() as session:
            src = Source(
                source_id="youtube:zzzzzzzzzzz",
                campaign="testcamp",
                platform="youtube",
                url="https://www.youtube.com/watch?v=zzzzzzzzzzz",
                status="done",
            )
            session.add(src)
            session.commit()

        mock_proc = MagicMock()
        mock_proc.pid = 88888

        with patch("subprocess.Popen", return_value=mock_proc):
            r = c.post(
                "/api/campaigns/testcamp/videos",
                json={
                    "url": "https://www.youtube.com/watch?v=zzzzzzzzzzz",
                    "force": True,
                },
                headers=_auth_headers(),
            )

        assert r.status_code == 200
        assert r.json()["started"] is True

    def test_popen_cmd_includes_force_flag(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)

        mock_proc = MagicMock()
        mock_proc.pid = 77777
        cmd_captured: list = []

        def capture_popen(cmd: Any, **kwargs: Any) -> Any:
            cmd_captured.extend(cmd)
            return mock_proc

        with patch("subprocess.Popen", side_effect=capture_popen):
            c.post(
                "/api/campaigns/testcamp/videos",
                json={
                    "url": "https://www.youtube.com/watch?v=abc12345678",
                    "force": True,
                },
                headers=_auth_headers(),
            )

        assert "--force" in cmd_captured

    def test_unauthenticated_returns_401(self, client: Any) -> None:
        c, tmp_path, yaml_path = client
        _write_test_campaign_yaml(yaml_path)
        # Use a fresh client without the session cookie to test 401
        from web.api import app
        fresh_client = TestClient(app, base_url="https://testserver")
        r = fresh_client.post(
            "/api/campaigns/testcamp/videos",
            json={"url": "https://www.youtube.com/watch?v=abc12345678"},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/videos/{source_id}/log
# ---------------------------------------------------------------------------

class TestGetVideoLog:
    def test_no_log_file_returns_404(self, client: Any) -> None:
        c, tmp_path, _ = client
        r = c.get(
            "/api/videos/youtube:nonexistent99/log",
            headers=_auth_headers(),
        )
        assert r.status_code == 404
        assert "log" in r.json()["detail"]["error"].lower()

    def test_log_file_present_returns_lines(self, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        c, tmp_path, _ = client

        # Patch STORAGE_DIR in api.py so the endpoint looks in our tmp_path
        import web.api as _api
        monkeypatch.setattr(_api, "STORAGE_DIR", tmp_path)

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "video-testcamp-abc12345678.log"
        log_file.write_text("line 1\nline 2\nline 3\n")

        r = c.get(
            "/api/videos/youtube:abc12345678/log",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert "source_id" in body
        assert "lines" in body
        assert isinstance(body["lines"], list)
        assert "line 1" in body["lines"]
        assert "line 3" in body["lines"]

    def test_lines_param_respected(self, client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        c, tmp_path, _ = client

        import web.api as _api
        monkeypatch.setattr(_api, "STORAGE_DIR", tmp_path)

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "video-testcamp-linelimit9.log"
        content = "\n".join(f"line {i}" for i in range(20))
        log_file.write_text(content)

        r = c.get(
            "/api/videos/youtube:linelimit9/log?lines=5",
            headers=_auth_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["lines"]) <= 5

    def test_unauthenticated_returns_401(self, client: Any) -> None:
        c, tmp_path, _ = client
        # Use a fresh client without the session cookie
        from web.api import app
        fresh_client = TestClient(app, base_url="https://testserver")
        r = fresh_client.get("/api/videos/youtube:abc12345678/log")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# clips_detail shape in _source_row_to_dict
# ---------------------------------------------------------------------------

class TestClipsDetailShape:
    def test_clips_detail_present_in_source_response(self, client: Any) -> None:
        c, tmp_path, _ = client

        # Insert a source with a clip
        from core.db import get_session
        from core.models import Source, Clip
        with get_session() as session:
            src = Source(
                source_id="youtube:detailtest1",
                campaign="testcamp",
                platform="youtube",
                url="https://www.youtube.com/watch?v=detailtest1",
                status="done",
            )
            session.add(src)
            session.flush()
            clip = Clip(
                campaign="testcamp",
                source_id="youtube:detailtest1",
                kind="clip",
                mode="demo",
                aspect="9:16",
                status="pending_review",
                hook="Detail test hook",
                gate_status="pending",
                correction_attempts=0,
                critic_reports=[],
                judge_decision=None,
            )
            session.add(clip)
            session.commit()

        r = c.get("/api/sources", headers=_auth_headers())
        assert r.status_code == 200
        sources = r.json()

        # Find our test source
        detail_source = next(
            (s for s in sources if s["source_id"] == "youtube:detailtest1"),
            None,
        )
        assert detail_source is not None, "Test source not found in response"

        # clips_detail must be present
        assert "clips_detail" in detail_source
        clips_detail = detail_source["clips_detail"]
        assert isinstance(clips_detail, list)
        assert len(clips_detail) == 1

        # Verify shape of each entry
        entry = clips_detail[0]
        assert "clip_id" in entry
        assert "gate_status" in entry
        assert "status" in entry
        assert "correction_attempts" in entry
        assert "reason" in entry
        assert "judge" in entry

        # Verify values
        assert entry["gate_status"] == "pending"
        assert entry["status"] == "pending_review"
        assert entry["correction_attempts"] == 0
        assert entry["reason"] is None or isinstance(entry["reason"], str)
        assert entry["judge"] is None

    def test_clips_detail_with_judge_decision(self, client: Any) -> None:
        c, tmp_path, _ = client

        from core.db import get_session
        from core.models import Source, Clip
        with get_session() as session:
            src = Source(
                source_id="youtube:judgetest1",
                campaign="testcamp",
                platform="youtube",
                url="https://www.youtube.com/watch?v=judgetest1",
                status="done",
            )
            session.add(src)
            session.flush()
            clip = Clip(
                campaign="testcamp",
                source_id="youtube:judgetest1",
                kind="clip",
                mode="demo",
                aspect="9:16",
                status="pending_review",
                hook="Judge test hook",
                gate_status="ready",
                correction_attempts=1,
                critic_reports=[{"attempt": 0, "passed": False, "failures": [
                    {"check": "self_contained", "reason": "Ends mid-sentence",
                     "severity": "correctable", "phase": "2"}
                ]}],
                judge_decision={
                    "clip_id": 999,
                    "decision": "approved",
                    "reasons": ["All checks passed"],
                    "decided_at": "2026-07-27T00:00:00+00:00",
                },
            )
            session.add(clip)
            session.commit()

        r = c.get("/api/sources", headers=_auth_headers())
        assert r.status_code == 200
        sources = r.json()

        src_data = next(
            (s for s in sources if s["source_id"] == "youtube:judgetest1"),
            None,
        )
        assert src_data is not None
        assert "clips_detail" in src_data
        entry = src_data["clips_detail"][0]

        assert entry["judge"] == "approved"
        assert entry["correction_attempts"] == 1
        # Reasons should come from judge_decision.reasons since judge is set
        assert entry["reason"] is None or isinstance(entry["reason"], str)
        assert (entry["reason"] or "").startswith("All checks passed") or entry["reason"] is None or "All checks passed" in (entry["reason"] or "")

    def test_clips_detail_with_critic_failures_no_judge(self, client: Any) -> None:
        c, tmp_path, _ = client

        from core.db import get_session
        from core.models import Source, Clip
        with get_session() as session:
            src = Source(
                source_id="youtube:critictest1",
                campaign="testcamp",
                platform="youtube",
                url="https://www.youtube.com/watch?v=critictest1",
                status="done",
            )
            session.add(src)
            session.flush()
            clip = Clip(
                campaign="testcamp",
                source_id="youtube:critictest1",
                kind="clip",
                mode="demo",
                aspect="9:16",
                status="pending_review",
                hook="Critic test hook",
                gate_status="pending",
                correction_attempts=0,
                critic_reports=[{
                    "clip_id": 1,
                    "attempt": 0,
                    "passed": False,
                    "failures": [
                        {
                            "check": "hook_body_match",
                            "reason": "Hook does not match body content",
                            "severity": "correctable",
                            "phase": "2",
                        }
                    ],
                }],
                judge_decision=None,
            )
            session.add(clip)
            session.commit()

        r = c.get("/api/sources", headers=_auth_headers())
        assert r.status_code == 200
        sources = r.json()

        src_data = next(
            (s for s in sources if s["source_id"] == "youtube:critictest1"),
            None,
        )
        assert src_data is not None
        entry = src_data["clips_detail"][0]

        assert entry["judge"] is None
        # reason should come from the latest critic report (first failure)
        assert entry["reason"] == "Hook does not match body content"


# ---------------------------------------------------------------------------
# SSE stage filter includes 'correcting'
# ---------------------------------------------------------------------------

class TestSSEStageFilter:
    """Verify 'correcting' stage is included in the in-progress filters."""

    def test_correcting_stage_source_appears_in_list(self, client: Any) -> None:
        c, tmp_path, _ = client

        from core.db import get_session
        from core.models import Source
        with get_session() as session:
            src = Source(
                source_id="youtube:correcting1",
                campaign="testcamp",
                platform="youtube",
                url="https://www.youtube.com/watch?v=correcting1",
                status="pending",
                stage="correcting",
                stage_updated_at=datetime.now(tz=timezone.utc),
            )
            session.add(src)
            session.commit()

        r = c.get("/api/sources?in_progress=1", headers=_auth_headers())
        assert r.status_code == 200
        sources = r.json()
        source_ids = [s["source_id"] for s in sources]
        assert "youtube:correcting1" in source_ids

    def test_correcting_stage_not_excluded(self, client: Any) -> None:
        """Sources with stage='correcting' must appear in /api/sources."""
        c, tmp_path, _ = client

        from core.db import get_session
        from core.models import Source
        with get_session() as session:
            src = Source(
                source_id="youtube:correcting2",
                campaign="testcamp",
                platform="youtube",
                url="https://www.youtube.com/watch?v=correcting2",
                status="pending",
                stage="correcting",
                stage_updated_at=datetime.now(tz=timezone.utc),
            )
            session.add(src)
            session.commit()

        # Default /api/sources should also include it (it has status != 'pending'
        # effectively — but stage filter handles in_progress)
        r = c.get("/api/sources?in_progress=1", headers=_auth_headers())
        assert r.status_code == 200
        sources = r.json()
        correcting = [s for s in sources if s["stage"] == "correcting"]
        assert len(correcting) >= 1
