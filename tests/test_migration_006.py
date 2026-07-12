"""
tests/test_migration_006.py — Verify migration 006 and Transcript.sentences model.

Tests:
- Transcript model has the sentences column with correct nullability
- Transcript instantiation with sentences=None (default)
- Transcript instantiation with sentences=[{...}] list (cached spans)
- Migration 006 meta: revision / down_revision / upgrade/downgrade importable
"""

from __future__ import annotations

import importlib
import importlib.util
import os

import pytest

from core.models import Transcript


def _load_migration_006():
    """Load the migration 006 module regardless of Python module path."""
    path = os.path.join(
        os.path.dirname(__file__),
        "..", "migrations", "versions", "006_sentence_cache.py",
    )
    spec = importlib.util.spec_from_file_location("m006", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


class TestTranscriptSentencesColumn:
    def test_sentences_attribute_exists(self):
        """Transcript model has a sentences attribute."""
        assert hasattr(Transcript, "sentences"), "Transcript must have a 'sentences' attribute"

    def test_sentences_nullable(self):
        """The sentences column must be nullable (JSONB NULL)."""
        col = Transcript.__table__.c.get("sentences")
        assert col is not None, "Transcript table must have a 'sentences' column"
        assert col.nullable is True, "sentences column must be nullable"

    def test_transcript_instantiates_with_none_sentences(self):
        """Transcript can be instantiated with sentences=None."""
        tr = Transcript(
            source_id="youtube:test123",
            segments=[{"start": 0.0, "end": 5.0, "text": "Hello world"}],
            word_level=False,
            sentences=None,
        )
        assert tr.sentences is None

    def test_transcript_instantiates_with_sentences_list(self):
        """Transcript can be instantiated with a list of sentence spans."""
        spans = [
            {"text": "Hello world.", "start": 0.0, "end": 5.0},
            {"text": "How are you?", "start": 5.0, "end": 10.0},
        ]
        tr = Transcript(
            source_id="youtube:test456",
            segments=[{"start": 0.0, "end": 10.0, "text": "Hello world how are you"}],
            word_level=False,
            sentences=spans,
        )
        assert tr.sentences == spans
        assert len(tr.sentences) == 2
        assert tr.sentences[0]["text"] == "Hello world."

    def test_transcript_sentences_roundtrip(self):
        """sentences value survives a round-trip through the model attribute."""
        original = [
            {"text": "First sentence.", "start": 0.0, "end": 5.0},
            {"text": "Second sentence.", "start": 5.2, "end": 11.0},
        ]
        tr = Transcript(
            source_id="youtube:roundtrip",
            segments=[],
            sentences=original,
        )
        assert tr.sentences[0]["start"] == 0.0
        assert tr.sentences[1]["end"] == 11.0


class TestMigration006Meta:
    def test_revision_ids(self):
        """Migration 006 has the correct revision and down_revision."""
        m = _load_migration_006()
        assert m.revision == "006"
        assert m.down_revision == "005"

    def test_has_upgrade_function(self):
        """Migration 006 has an upgrade() function."""
        m = _load_migration_006()
        assert callable(m.upgrade)

    def test_has_downgrade_function(self):
        """Migration 006 has a downgrade() function."""
        m = _load_migration_006()
        assert callable(m.downgrade)

    def test_jsonb_helper_returns_type(self):
        """The _jsonb() helper returns a valid SQLAlchemy type."""
        import sqlalchemy as sa
        m = _load_migration_006()
        t = m._jsonb()
        assert isinstance(t, sa.types.TypeEngine)
