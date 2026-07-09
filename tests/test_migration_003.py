"""
tests/test_migration_003.py — Verify migration 003 fields exist on the Clip model.

These tests check ORM-model parity with migration 003 (gate_status, gate_reasons,
formula_score).  No DB connection required — tests inspect the SQLAlchemy Table
column definitions directly.
"""

from __future__ import annotations

import pytest


class TestMigration003ModelParity:
    """Clip model must have all columns defined in migration 003."""

    def test_clip_has_gate_status(self):
        from core.models import Clip
        assert hasattr(Clip, "gate_status"), (
            "Clip must have 'gate_status' column (migration 003)"
        )

    def test_clip_has_gate_reasons(self):
        from core.models import Clip
        assert hasattr(Clip, "gate_reasons"), (
            "Clip must have 'gate_reasons' column (migration 003)"
        )

    def test_clip_has_formula_score(self):
        from core.models import Clip
        assert hasattr(Clip, "formula_score"), (
            "Clip must have 'formula_score' column (migration 003)"
        )

    def test_gate_status_default_is_pending(self):
        """gate_status must default to 'pending' (Python or server default)."""
        from core.models import Clip
        col = Clip.__table__.c.gate_status
        # Column exists and is not nullable
        assert not col.nullable
        # Check Python-level default (mapped_column(default='pending'))
        py_default = col.default
        if py_default is not None:
            assert "pending" in str(py_default.arg)
        else:
            # Alternatively the migration sets a server_default
            server_default = col.server_default
            assert server_default is not None, (
                "gate_status must have either a Python default or server_default of 'pending'"
            )
            assert "pending" in str(server_default.arg)

    def test_gate_reasons_is_nullable(self):
        """gate_reasons is a nullable JSONB column (not populated until gate runs)."""
        from core.models import Clip
        col = Clip.__table__.c.gate_reasons
        assert col.nullable

    def test_formula_score_is_nullable(self):
        """formula_score is nullable (None until Phase 2 runs)."""
        from core.models import Clip
        col = Clip.__table__.c.formula_score
        assert col.nullable

    def test_gate_status_index_exists(self):
        """ix_clips_gate_status index must be defined on Clip.__table_args__."""
        from core.models import Clip
        index_names = {idx.name for idx in Clip.__table__.indexes}
        assert "ix_clips_gate_status" in index_names, (
            "Index ix_clips_gate_status must exist on clips table (migration 003)"
        )

    def test_gate_status_string_length(self):
        """gate_status must be String(16) — long enough for all status values."""
        from core.models import Clip
        import sqlalchemy as sa
        col = Clip.__table__.c.gate_status
        # Accept either String or VARCHAR
        assert isinstance(col.type, sa.String)
        assert col.type.length >= 16


class TestMigration003GateResultRoundtrip:
    """Clip can be instantiated with gate fields and they round-trip correctly."""

    def test_clip_instantiation_with_gate_fields(self):
        from core.models import Clip
        clip = Clip(
            campaign="test",
            kind="clip",
            mode="demo",
            aspect="9:16",
            gate_status="ready",
            gate_reasons=[
                {"phase": "1", "check": "resolution", "pass": True, "reason": "1080x1920 OK"}
            ],
            formula_score=0.75,
        )
        assert clip.gate_status == "ready"
        assert isinstance(clip.gate_reasons, list)
        assert clip.formula_score == 0.75

    def test_clip_didnt_pass_status(self):
        from core.models import Clip
        clip = Clip(campaign="test", gate_status="didnt_pass")
        assert clip.gate_status == "didnt_pass"

    def test_clip_overridden_status(self):
        from core.models import Clip
        clip = Clip(campaign="test", gate_status="overridden")
        assert clip.gate_status == "overridden"
