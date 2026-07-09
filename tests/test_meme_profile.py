"""
tests/test_meme_profile.py — unit tests for meme/profile.py

Covers:
  - Profile JSON validation and normalisation (_validate_profile)
  - extract_profile: version incrementing, empty refs_dir, LLM parse
  - get_active_profile: latest-version retrieval
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# SQLite session fixture (reused across profile tests)
# ---------------------------------------------------------------------------

@pytest.fixture
def sqlite_session(monkeypatch):
    """In-memory SQLite session with full schema."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.models import Base

    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "claude-test")

    from core.settings import get_settings
    get_settings.cache_clear()

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()

    yield session

    session.close()
    engine.dispose()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Minimal valid profile dict for testing
# ---------------------------------------------------------------------------

_VALID_PROFILE = {
    "aspect": "1:1",
    "visual_format": {
        "layout": "text overlay on bold image",
        "image_style": "photography",
        "text_placement": "center",
        "colors": ["black", "white"],
        "typical_composition": "subject fills frame",
    },
    "caption_voice": {
        "tone": "direct",
        "person": "second",
        "sentence_length": "short",
        "punctuation_habits": "no periods",
        "slang_level": "low",
    },
    "measurable_rules": [
        {"rule": "caption under 10 words", "confidence": 0.95},
        {"rule": "use high-contrast background", "confidence": 0.85},
    ],
}

_LLM_PROFILE_RESPONSE = json.dumps(_VALID_PROFILE)


# ---------------------------------------------------------------------------
# 1. _validate_profile
# ---------------------------------------------------------------------------

class TestValidateProfile:
    """_validate_profile normalises raw LLM dicts."""

    def _call(self, data: dict) -> dict:
        from meme.profile import _validate_profile
        return _validate_profile(data)

    def test_valid_profile_passes(self):
        result = self._call(dict(_VALID_PROFILE))
        assert result["aspect"] == "1:1"
        assert len(result["measurable_rules"]) == 2

    def test_missing_visual_format_raises(self):
        bad = {k: v for k, v in _VALID_PROFILE.items() if k != "visual_format"}
        with pytest.raises(ValueError, match="visual_format"):
            self._call(bad)

    def test_missing_caption_voice_raises(self):
        bad = {k: v for k, v in _VALID_PROFILE.items() if k != "caption_voice"}
        with pytest.raises(ValueError, match="caption_voice"):
            self._call(bad)

    def test_missing_measurable_rules_raises(self):
        bad = {k: v for k, v in _VALID_PROFILE.items() if k != "measurable_rules"}
        with pytest.raises(ValueError, match="measurable_rules"):
            self._call(bad)

    def test_unknown_aspect_defaults_to_1_1(self):
        data = dict(_VALID_PROFILE)
        data["aspect"] = "16:9"  # not a valid meme aspect
        result = self._call(data)
        assert result["aspect"] == "1:1"

    def test_aspect_4_5_preserved(self):
        data = dict(_VALID_PROFILE)
        data["aspect"] = "4:5"
        result = self._call(data)
        assert result["aspect"] == "4:5"

    def test_absent_aspect_defaults_to_1_1(self):
        data = {k: v for k, v in _VALID_PROFILE.items() if k != "aspect"}
        result = self._call(data)
        assert result["aspect"] == "1:1"

    def test_rules_confidence_clamped(self):
        data = dict(_VALID_PROFILE)
        data["measurable_rules"] = [
            {"rule": "test", "confidence": 2.5},   # above 1.0 → clamp to 1.0
            {"rule": "test2", "confidence": -0.1},  # below 0.0 → clamp to 0.0
        ]
        result = self._call(data)
        confidences = [r["confidence"] for r in result["measurable_rules"]]
        assert all(0.0 <= c <= 1.0 for c in confidences)

    def test_rules_without_rule_key_skipped(self):
        data = dict(_VALID_PROFILE)
        data["measurable_rules"] = [
            {"confidence": 0.8},         # no 'rule' key
            {"rule": "good rule", "confidence": 0.9},
        ]
        result = self._call(data)
        assert len(result["measurable_rules"]) == 1
        assert result["measurable_rules"][0]["rule"] == "good rule"

    def test_non_list_rules_replaced_with_empty(self):
        data = dict(_VALID_PROFILE)
        data["measurable_rules"] = "not a list"
        result = self._call(data)
        assert result["measurable_rules"] == []

    def test_visual_format_defaults_filled(self):
        data = dict(_VALID_PROFILE)
        data["visual_format"] = {}  # completely empty
        result = self._call(data)
        vf = result["visual_format"]
        assert "layout" in vf
        assert "colors" in vf
        assert isinstance(vf["colors"], list)

    def test_non_dict_visual_format_replaced(self):
        data = dict(_VALID_PROFILE)
        data["visual_format"] = "not a dict"
        result = self._call(data)
        assert isinstance(result["visual_format"], dict)


# ---------------------------------------------------------------------------
# 2. extract_profile
# ---------------------------------------------------------------------------

class TestExtractProfile:
    """extract_profile with mocked LLM vision calls."""

    @pytest.fixture(autouse=True)
    def _mock_llm(self, monkeypatch):
        """Mock call_vision to return the valid profile JSON string."""
        monkeypatch.setattr(
            "meme.profile.call_vision",
            lambda prompt, images, **kw: _LLM_PROFILE_RESPONSE,
        )

    def _make_cfg(self, refs_dir: Path, monkeypatch):
        """Build a minimal CampaignConfig pointing at refs_dir."""
        import textwrap
        from core.config import load_campaign

        yaml_content = textwrap.dedent(f"""
            name: test_campaign
            enabled: true
            engines:
              clips: false
              memes: true
            meme:
              refs_dir: "{refs_dir}"
              image_model: ""
              hard_rules: []
            sources:
              youtube:
                search_terms: ["test"]
            ranking:
              clip_length: [20, 60]
              ranking_rules: "test"
            template:
              captions:
                style: word_by_word
              hook: {{}}
              lower_third: {{}}
              watermark: {{}}
              corner_badge: {{}}
              outro:
                enabled: false
            destinations:
              postiz_channels: ["ch1"]
              schedule: {{}}
              caption_template: "{{hook}}"
              hashtags: []
            analytics: {{}}
        """)
        yaml_file = refs_dir.parent / "test_campaign.yaml"
        yaml_file.write_text(yaml_content)
        return load_campaign(yaml_file, strict_assets=False)

    def test_first_run_creates_version_1(self, tmp_path, sqlite_session, monkeypatch):
        """extract_profile on a fresh campaign creates version=1."""
        refs_dir = tmp_path / "meme_refs"
        refs_dir.mkdir()
        # Create a dummy PNG reference image
        (refs_dir / "ref1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

        cfg = self._make_cfg(refs_dir, monkeypatch)
        from meme.profile import extract_profile

        profile = extract_profile(cfg, sqlite_session)
        assert profile.version == 1
        assert profile.campaign == "test_campaign"
        assert "aspect" in profile.profile

    def test_version_increments_on_second_extract(
        self, tmp_path, sqlite_session, monkeypatch
    ):
        """Second extract_profile produces version=2."""
        refs_dir = tmp_path / "meme_refs"
        refs_dir.mkdir()
        (refs_dir / "ref1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

        cfg = self._make_cfg(refs_dir, monkeypatch)
        from meme.profile import extract_profile

        p1 = extract_profile(cfg, sqlite_session)
        sqlite_session.commit()
        p2 = extract_profile(cfg, sqlite_session)
        sqlite_session.commit()

        assert p2.version == 2
        assert p2.campaign == p1.campaign

    def test_empty_refs_dir_raises(self, tmp_path, sqlite_session, monkeypatch):
        """No reference images → ValueError with clear message."""
        refs_dir = tmp_path / "empty_refs"
        refs_dir.mkdir()

        cfg = self._make_cfg(refs_dir, monkeypatch)
        from meme.profile import extract_profile

        with pytest.raises(ValueError, match="no reference images|contains no"):
            extract_profile(cfg, sqlite_session)

    def test_nonexistent_refs_dir_raises(self, tmp_path, sqlite_session, monkeypatch):
        """Non-existent refs_dir → ValueError."""
        refs_dir = tmp_path / "nonexistent"
        # Do NOT create the dir

        cfg = self._make_cfg(refs_dir, monkeypatch)
        from meme.profile import extract_profile

        with pytest.raises(ValueError, match="does not exist"):
            extract_profile(cfg, sqlite_session)

    def test_missing_refs_dir_config_raises(self, tmp_path, sqlite_session):
        """CampaignConfig with no meme section → ValueError."""
        import textwrap
        from core.config import load_campaign

        yaml_content = textwrap.dedent("""
            name: no_meme_cfg
            enabled: true
            sources:
              youtube:
                search_terms: ["test"]
            ranking:
              clip_length: [20, 60]
              ranking_rules: "test"
            template:
              captions:
                style: word_by_word
              hook: {}
              lower_third: {}
              watermark: {}
              corner_badge: {}
              outro:
                enabled: false
            destinations:
              postiz_channels: ["ch1"]
              schedule: {}
              caption_template: "{hook}"
              hashtags: []
            analytics: {}
        """)
        yaml_file = tmp_path / "no_meme_cfg.yaml"
        yaml_file.write_text(yaml_content)
        cfg = load_campaign(yaml_file, strict_assets=False)

        from meme.profile import extract_profile

        with pytest.raises(ValueError, match="refs_dir"):
            extract_profile(cfg, sqlite_session)

    def test_llm_parse_failure_retries_then_raises(self, tmp_path, sqlite_session, monkeypatch):
        """If LLM returns non-JSON twice, extract_profile raises ValueError."""
        refs_dir = tmp_path / "meme_refs"
        refs_dir.mkdir()
        (refs_dir / "ref1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

        monkeypatch.setattr(
            "meme.profile.call_vision",
            lambda *a, **kw: "I cannot analyse these images.",
        )

        cfg = self._make_cfg(refs_dir, monkeypatch)
        from meme.profile import extract_profile

        with pytest.raises(ValueError, match="failed to return a valid profile"):
            extract_profile(cfg, sqlite_session)


# ---------------------------------------------------------------------------
# 3. get_active_profile
# ---------------------------------------------------------------------------

class TestGetActiveProfile:
    """get_active_profile returns the highest-version profile."""

    def test_returns_none_when_no_profiles(self, sqlite_session):
        from meme.profile import get_active_profile

        result = get_active_profile("nonexistent_campaign", sqlite_session)
        assert result is None

    def test_returns_latest_version(self, sqlite_session):
        """With v1 and v2 stored, returns v2."""
        from core.models import MemeProfile
        from meme.profile import get_active_profile

        now = datetime.now(timezone.utc)
        sqlite_session.add_all([
            MemeProfile(
                campaign="fitness",
                version=1,
                profile={"aspect": "1:1", "version_label": "first"},
                created_at=now,
                updated_at=now,
            ),
            MemeProfile(
                campaign="fitness",
                version=2,
                profile={"aspect": "4:5", "version_label": "second"},
                created_at=now,
                updated_at=now,
            ),
        ])
        sqlite_session.commit()

        result = get_active_profile("fitness", sqlite_session)
        assert result is not None
        assert result.version == 2
        assert result.profile["version_label"] == "second"

    def test_returns_correct_campaign(self, sqlite_session):
        """Does not confuse profiles across different campaigns."""
        from core.models import MemeProfile
        from meme.profile import get_active_profile

        now = datetime.now(timezone.utc)
        sqlite_session.add_all([
            MemeProfile(
                campaign="fitness",
                version=1,
                profile={"aspect": "1:1"},
                created_at=now,
                updated_at=now,
            ),
            MemeProfile(
                campaign="cooking",
                version=5,
                profile={"aspect": "4:5"},
                created_at=now,
                updated_at=now,
            ),
        ])
        sqlite_session.commit()

        fitness = get_active_profile("fitness", sqlite_session)
        cooking = get_active_profile("cooking", sqlite_session)

        assert fitness.version == 1
        assert cooking.version == 5
