"""
tests/test_meme_generate.py — integration tests for generate_memes and
generate_text_posts using an in-memory SQLite database.

All LLM calls, image client calls, and R2 operations are mocked.
No real network calls are made.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sqlite_session(monkeypatch, tmp_path):
    """In-memory SQLite session with full schema + minimal env vars."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.models import Base

    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "claude-test")
    monkeypatch.setenv("MEME_IMAGE_MODEL", "test/image-model")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))

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


@pytest.fixture
def campaign_cfg(tmp_path):
    """A CampaignConfig with engines.memes=True and a populated refs_dir."""
    import textwrap
    from core.config import load_campaign

    refs_dir = tmp_path / "meme_refs"
    refs_dir.mkdir()
    (refs_dir / "ref1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    yaml_content = textwrap.dedent(f"""
        name: test_gen
        enabled: true
        mode: demo
        engines:
          clips: false
          memes: true
        meme:
          refs_dir: "{refs_dir}"
          image_model: ""
          hard_rules: []
        creative_direction: "Make fitness memes"
        sources:
          youtube:
            search_terms: ["fitness"]
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
          postiz_channels: ["ch_fitness"]
          schedule: {{}}
          caption_template: "{{hook}}"
          hashtags: []
        analytics: {{}}
    """)
    yaml_file = tmp_path / "test_gen.yaml"
    yaml_file.write_text(yaml_content)
    return load_campaign(yaml_file, strict_assets=False)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

_PROFILE_JSON = json.dumps({
    "aspect": "1:1",
    "visual_format": {
        "layout": "bold text on image",
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
        {"rule": "caption under 10 words", "confidence": 0.9},
    ],
})

_CONCEPT_JSON = json.dumps({
    "concept": "A person lifting heavy weights with a determined expression",
    "caption": "No excuses. Just gains.",
})

_GOOD_CLASSIFIER_JSON = json.dumps({
    "on_format": 0.9,
    "on_voice": 0.85,
    "on_brand": 0.88,
    "legibility": 0.92,
    "compliance": 1.0,
    "reasons": ["looks great"],
})

_FAIL_CLASSIFIER_JSON = json.dumps({
    "on_format": 0.1,
    "on_voice": 0.2,
    "on_brand": 0.1,
    "legibility": 0.2,
    "compliance": 0.0,
    "reasons": ["fails compliance"],
})

_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


def _mock_image_client_factory(monkeypatch):
    """Patch build_image_client to return a fake client that returns _FAKE_PNG."""
    class _FakeImageClient:
        def generate(self, prompt, refs):
            return _FAKE_PNG

    monkeypatch.setattr(
        "meme.image_client.build_image_client",
        lambda model: _FakeImageClient(),
    )


def _mock_llm_calls(monkeypatch, *, pass_classifier=True):
    """Patch all LLM call functions used by generate_memes."""

    classifier_json = _GOOD_CLASSIFIER_JSON if pass_classifier else _FAIL_CLASSIFIER_JSON

    def _text_call(prompt, **kw):
        # Called for concept generation
        return _CONCEPT_JSON

    def _vision_call(prompt, images, **kw):
        # Called for profile extraction AND classifier
        if "judge" in prompt.lower() or "score" in prompt.lower() or "compliance" in prompt.lower():
            return classifier_json
        return _PROFILE_JSON

    monkeypatch.setattr("meme.generate.call_text", _text_call)
    monkeypatch.setattr("meme.classifier.call_vision", _vision_call)
    monkeypatch.setattr("meme.profile.call_vision", lambda *a, **kw: _PROFILE_JSON)


# ---------------------------------------------------------------------------
# 1. generate_memes — Clip row shape
# ---------------------------------------------------------------------------

class TestGenerateMemes:
    """generate_memes inserts correct Clip rows on SQLite."""

    def test_inserts_clip_rows(
        self, monkeypatch, sqlite_session, campaign_cfg, tmp_path
    ):
        """Generating 2 memes inserts 2 Clip rows."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from meme.generate import generate_memes

        ids = generate_memes(campaign_cfg, 2, sqlite_session)
        sqlite_session.commit()

        assert len(ids) == 2

    def test_clip_kind_is_meme(
        self, monkeypatch, sqlite_session, campaign_cfg, tmp_path
    ):
        """Inserted Clip rows have kind='meme'."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from core.models import Clip
        from meme.generate import generate_memes

        ids = generate_memes(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip is not None
        assert clip.kind == "meme"

    def test_clip_mode_stamped_from_campaign(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """Clip.mode is stamped from campaign mode (demo)."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from core.models import Clip
        from meme.generate import generate_memes

        ids = generate_memes(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.mode == "demo"

    def test_clip_mode_override(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """mode_override='production' overrides campaign mode."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from core.models import Clip
        from meme.generate import generate_memes

        ids = generate_memes(
            campaign_cfg, 1, sqlite_session, mode_override="production"
        )
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.mode == "production"

    def test_clip_aspect_from_profile(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """Clip.aspect matches the profile's aspect (1:1 from _PROFILE_JSON)."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from core.models import Clip
        from meme.generate import generate_memes

        ids = generate_memes(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.aspect == "1:1"

    def test_clip_status_pending_review_when_pass(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """When classifier passes, status='pending_review'."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from core.models import Clip
        from meme.generate import generate_memes

        ids = generate_memes(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.status == "pending_review"

    def test_clip_status_rejected_when_fail(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """When classifier fails, status='rejected' with reject_reason set."""
        _mock_llm_calls(monkeypatch, pass_classifier=False)
        _mock_image_client_factory(monkeypatch)

        from core.models import Clip
        from meme.generate import generate_memes

        ids = generate_memes(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.status == "rejected"
        assert clip.reject_reason is not None and len(clip.reject_reason) > 0

    def test_clip_meme_meta_contains_required_keys(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """meme_meta has concept, classifier_scores, profile_version."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from core.models import Clip
        from meme.generate import generate_memes

        ids = generate_memes(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.meme_meta is not None
        assert "concept" in clip.meme_meta
        assert "classifier_scores" in clip.meme_meta
        assert "profile_version" in clip.meme_meta

    def test_clip_source_id_is_null(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """Meme clips have no source video; source_id must be NULL."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from core.models import Clip
        from meme.generate import generate_memes

        ids = generate_memes(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.source_id is None
        assert clip.start is None
        assert clip.end is None

    def test_clip_file_path_set_after_image_save(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """Clip.file_path is set (not None) after the image is saved."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from core.models import Clip
        from meme.generate import generate_memes

        ids = generate_memes(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.file_path is not None
        assert len(clip.file_path) > 0

    def test_profile_extracted_on_first_run(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """If no profile exists yet, generate_memes extracts one automatically."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from core.models import MemeProfile
        from meme.generate import generate_memes

        # No profile exists before the call
        count_before = sqlite_session.query(MemeProfile).count()
        assert count_before == 0

        generate_memes(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        count_after = sqlite_session.query(MemeProfile).count()
        assert count_after == 1

    def test_destination_channels_set(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """Clip.destination_channels mirrors the campaign's postiz_channels."""
        _mock_llm_calls(monkeypatch, pass_classifier=True)
        _mock_image_client_factory(monkeypatch)

        from core.models import Clip
        from meme.generate import generate_memes

        ids = generate_memes(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.destination_channels == ["ch_fitness"]


# ---------------------------------------------------------------------------
# 2. generate_text_posts — text card + voice-only classifier
# ---------------------------------------------------------------------------

class TestGenerateTextPosts:
    """generate_text_posts inserts Clip rows with text_only=True in meme_meta."""

    def test_inserts_clip_rows(
        self, monkeypatch, sqlite_session, campaign_cfg, tmp_path
    ):
        """Generating 2 text posts inserts 2 Clip rows."""
        # Mock caption generation
        monkeypatch.setattr(
            "meme.text_posts.call_text",
            lambda *a, **kw: json.dumps({"caption": "Hit your goals today"}),
        )
        # Mock profile extraction (vision call for profile)
        monkeypatch.setattr(
            "meme.profile.call_vision",
            lambda *a, **kw: _PROFILE_JSON,
        )
        # Mock classifier (text-only → call_text)
        monkeypatch.setattr(
            "meme.classifier.call_text",
            lambda *a, **kw: json.dumps({
                "on_voice": 0.9,
                "compliance": 1.0,
                "reasons": ["great voice"],
            }),
        )

        from meme.text_posts import generate_text_posts

        ids = generate_text_posts(campaign_cfg, 2, sqlite_session)
        sqlite_session.commit()

        assert len(ids) == 2

    def test_meme_meta_text_only_flag(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """meme_meta[text_only] must be True."""
        monkeypatch.setattr(
            "meme.text_posts.call_text",
            lambda *a, **kw: json.dumps({"caption": "Stay consistent"}),
        )
        monkeypatch.setattr(
            "meme.profile.call_vision",
            lambda *a, **kw: _PROFILE_JSON,
        )
        monkeypatch.setattr(
            "meme.classifier.call_text",
            lambda *a, **kw: json.dumps({
                "on_voice": 0.9,
                "compliance": 1.0,
                "reasons": [],
            }),
        )

        from core.models import Clip
        from meme.text_posts import generate_text_posts

        ids = generate_text_posts(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.meme_meta is not None
        assert clip.meme_meta.get("text_only") is True

    def test_aspect_is_1_1(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """Text posts always use aspect='1:1'."""
        monkeypatch.setattr(
            "meme.text_posts.call_text",
            lambda *a, **kw: json.dumps({"caption": "Push harder"}),
        )
        monkeypatch.setattr(
            "meme.profile.call_vision",
            lambda *a, **kw: _PROFILE_JSON,
        )
        monkeypatch.setattr(
            "meme.classifier.call_text",
            lambda *a, **kw: json.dumps({
                "on_voice": 0.9,
                "compliance": 1.0,
                "reasons": [],
            }),
        )

        from core.models import Clip
        from meme.text_posts import generate_text_posts

        ids = generate_text_posts(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.aspect == "1:1"

    def test_file_path_set_to_text_card(
        self, monkeypatch, sqlite_session, campaign_cfg
    ):
        """file_path is set (text card PNG was saved)."""
        monkeypatch.setattr(
            "meme.text_posts.call_text",
            lambda *a, **kw: json.dumps({"caption": "Work for it"}),
        )
        monkeypatch.setattr(
            "meme.profile.call_vision",
            lambda *a, **kw: _PROFILE_JSON,
        )
        monkeypatch.setattr(
            "meme.classifier.call_text",
            lambda *a, **kw: json.dumps({
                "on_voice": 0.9,
                "compliance": 1.0,
                "reasons": [],
            }),
        )

        from core.models import Clip
        from meme.text_posts import generate_text_posts

        ids = generate_text_posts(campaign_cfg, 1, sqlite_session)
        sqlite_session.commit()

        clip = sqlite_session.query(Clip).filter(Clip.id == ids[0]).first()
        assert clip.file_path is not None


# ---------------------------------------------------------------------------
# 3. render_text_card
# ---------------------------------------------------------------------------

class TestRenderTextCard:
    """render_text_card produces valid PNG bytes."""

    def test_returns_png_bytes(self):
        from meme.text_posts import render_text_card

        result = render_text_card("Hello world")
        assert isinstance(result, bytes)
        # PNG magic bytes
        assert result[:4] == b"\x89PNG"

    def test_default_dimensions(self):
        """Default card is 1080x1080."""
        from PIL import Image
        import io
        from meme.text_posts import render_text_card

        result = render_text_card("Test caption for sizing")
        img = Image.open(io.BytesIO(result))
        assert img.size == (1080, 1080)

    def test_custom_dimensions(self):
        from PIL import Image
        import io
        from meme.text_posts import render_text_card

        result = render_text_card("Test", width=512, height=512)
        img = Image.open(io.BytesIO(result))
        assert img.size == (512, 512)

    def test_long_caption_does_not_raise(self):
        """A very long caption should wrap without error."""
        from meme.text_posts import render_text_card

        long_cap = "This is a very long caption " * 10
        result = render_text_card(long_cap)
        assert result[:4] == b"\x89PNG"

    def test_empty_caption_does_not_raise(self):
        from meme.text_posts import render_text_card

        result = render_text_card("")
        assert result[:4] == b"\x89PNG"
