"""
tests/test_campaign_update_merge.py — PUT /api/campaigns/{name} must deep-merge
onto the existing YAML (wizard saves must not 422 on missing sections nor wipe
fields the wizard doesn't manage). Regression for the operator's
"Failed: [object Object]" save bug (2026-07-28).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import web.api as web_api

CAMPAIGNS_DIR = Path(web_api.__file__).resolve().parent.parent / "campaigns"
SLUG = "mergetest"


WIZARD_CONFIG = {
    # Deliberately NO analytics, NO gate, NO ranking.stance — the wizard
    # never sends these.
    "name": SLUG,
    "enabled": True,
    "mode": "demo",
    "engines": {"clips": True, "memes": False},
    "creative_direction": "",
    "sources": {
        "youtube": {
            "search_terms": ["wizard term"],
            "channels": [],
            "min_view_count": 20000,
            "uploaded_within": "year",
        },
        "tiktok": {"profiles": [], "hashtags": []},
        "instagram": {"profiles": []},
    },
    "ranking": {
        "clip_length": [20, 60],
        "max_clips_per_source": 8,
        "exhaust_source": False,
        "min_score": 0.6,
        "ranking_rules": "wizard rules",
    },
    "template": {
        "aspect": "9:16",
        "resolution": [1080, 1920],
        "captions": {
            "style": "word_by_word",
            "base_color": "#FFFFFF",
            "highlight_color": "#00E5FF",
            "outline_color": "#000000",
            "outline_px": 6,
            "position": "upper_mid",
            "max_words_per_line": 4,
        },
        "hook": {"enabled": True, "show_seconds": [0, 8], "source": "ranking"},
        "lower_third": {"show_source_handle": True, "format": "via @{source_handle}"},
        "watermark": {"position": "center", "opacity": 0.18, "scale": 0.5},
        "corner_badge": {"position": "top_right", "opacity": 1.0, "scale": 0.12},
        "outro": {"enabled": False, "audio": "keep"},
    },
    "destinations": {
        "postiz_channels": ["x"],
        "schedule": {"posts_per_day": 1, "times": ["17:00"], "timezone": "America/New_York"},
        "caption_template": "",
        "hashtags": [],
        "autopost": False,
    },
    "demo": {"test_channels": []},
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "testpw")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/merge.db")
    c = TestClient(web_api.app, base_url="https://testserver")
    c.headers["Authorization"] = "Bearer testpw"
    yield c


@pytest.fixture()
def seeded_yaml():
    """A campaign yaml with advanced fields the wizard never sends."""
    seeded = {
        **json.loads(json.dumps(WIZARD_CONFIG)),
        "analytics": {"track": True, "pull_day": "friday"},
        "gate": {"relaxed_safety_checks": ["medical_claims"]},
    }
    seeded["ranking"] = {
        **seeded["ranking"],
        "ranking_rules": "SEEDED advanced rules",
        "stance": "pro-thing stance",
    }
    seeded["sources"] = {
        **seeded["sources"],
        "exclude_keywords": ["badword"],
    }
    path = CAMPAIGNS_DIR / f"{SLUG}.yaml"
    path.write_text(yaml.safe_dump(seeded, sort_keys=False))
    yield path
    path.unlink(missing_ok=True)


def test_wizard_save_does_not_422_and_preserves_advanced_fields(client, seeded_yaml):
    r = client.put(
        f"/api/campaigns/{SLUG}", data={"config": json.dumps(WIZARD_CONFIG)}
    )
    assert r.status_code == 200, r.text
    after = yaml.safe_load(seeded_yaml.read_text())
    # Wizard-managed field updated:
    assert after["ranking"]["ranking_rules"] == "wizard rules"
    # Fields the wizard never sends survive:
    assert after["ranking"]["stance"] == "pro-thing stance"
    assert after["gate"]["relaxed_safety_checks"] == ["medical_claims"]
    assert after["sources"]["exclude_keywords"] == ["badword"]
    assert after["analytics"]["pull_day"] == "friday"


def test_analytics_now_optional_in_config_model():
    from core.config import CampaignConfig

    cfg = CampaignConfig.model_validate(
        {**json.loads(json.dumps(WIZARD_CONFIG)), "name": "noanalytics"}
    )
    assert cfg.analytics.track is True


def test_deep_merge_semantics():
    from web.api import _deep_merge_config

    base = {"a": {"x": 1, "y": 2}, "keep": "me", "lst": [1, 2]}
    incoming = {"a": {"x": 9}, "lst": [3]}
    out = _deep_merge_config(base, incoming)
    assert out == {"a": {"x": 9, "y": 2}, "keep": "me", "lst": [3]}
    assert base == {"a": {"x": 1, "y": 2}, "keep": "me", "lst": [1, 2]}  # no mutation
