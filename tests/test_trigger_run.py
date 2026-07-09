"""Regression tests for POST /api/runs/{campaign} spend-cap enforcement.

The on-demand web trigger must NEVER spawn an uncapped producer run (spec §9).
These tests call ``trigger_run`` directly (the suite has no HTTP TestClient) with
``subprocess.Popen`` monkeypatched so nothing is actually spawned.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from web import api as web_api


class _FakeProc:
    pid = 4242


@pytest.fixture
def capture_cmd(monkeypatch):
    """Monkeypatch subprocess.Popen; return a list that collects the argv used."""
    captured: list[list[str]] = []

    def _fake_popen(cmd, *args, **kwargs):
        captured.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(web_api.subprocess, "Popen", _fake_popen)
    return captured


def _flag(cmd: list[str], name: str) -> str | None:
    """Return the value following ``name`` in an argv list, or None."""
    return cmd[cmd.index(name) + 1] if name in cmd else None


def test_default_run_is_capped(capture_cmd):
    """With no body, both spend caps must be applied at the demo defaults."""
    result = web_api.trigger_run("fitness")
    assert result["started"] is True
    cmd = capture_cmd[0]
    assert _flag(cmd, "--max-apify-spend") == str(web_api.DEFAULT_ONDEMAND_APIFY_SPEND)
    assert _flag(cmd, "--max-modal-spend") == str(web_api.DEFAULT_ONDEMAND_MODAL_SPEND)
    # No mode override → producer falls back to campaign cfg.mode.
    assert "--mode" not in cmd


def test_body_overrides_caps_and_mode(capture_cmd):
    web_api.trigger_run(
        "fitness",
        {"mode": "production", "max_apify_spend": 5, "max_modal_spend": 7.5},
    )
    cmd = capture_cmd[0]
    assert _flag(cmd, "--max-apify-spend") == "5.0"
    assert _flag(cmd, "--max-modal-spend") == "7.5"
    assert _flag(cmd, "--mode") == "production"


def test_invalid_mode_rejected(capture_cmd):
    with pytest.raises(HTTPException) as exc:
        web_api.trigger_run("fitness", {"mode": "turbo"})
    assert exc.value.status_code == 422
    assert not capture_cmd  # never spawned


@pytest.mark.parametrize("bad", [0, -1, "abc"])
def test_nonpositive_or_nonnumeric_cap_rejected(capture_cmd, bad):
    with pytest.raises(HTTPException) as exc:
        web_api.trigger_run("fitness", {"max_modal_spend": bad})
    assert exc.value.status_code == 422
    assert not capture_cmd


def test_unknown_campaign_404(capture_cmd):
    with pytest.raises(HTTPException) as exc:
        web_api.trigger_run("does-not-exist-xyz")
    assert exc.value.status_code == 404
    assert not capture_cmd
