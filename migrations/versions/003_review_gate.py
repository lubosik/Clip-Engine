"""Review gate — gate_status, gate_reasons, formula_score on clips.

Revision ID: 003
Revises: 002
Create Date: 2026-07-09 12:00:00.000000

Downgrade note: all gate_status / gate_reasons / formula_score data is
permanently lost on downgrade; the clips themselves are unaffected.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # clips — AI review gate fields                                        #
    # ------------------------------------------------------------------ #
    op.add_column(
        "clips",
        sa.Column(
            "gate_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment="pending | ready | didnt_pass | overridden",
        ),
    )
    op.add_column(
        "clips",
        sa.Column(
            "gate_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="List of {phase, check, pass, reason} dicts from run_gate()",
        ),
    )
    op.add_column(
        "clips",
        sa.Column(
            "formula_score",
            sa.Float(),
            nullable=True,
            comment="0.0-1.0 average of the §6c 10-question rubric; NULL until Phase 2 runs",
        ),
    )

    # Index for gate_status so the frontend can filter efficiently
    op.create_index("ix_clips_gate_status", "clips", ["gate_status"])


def downgrade() -> None:
    op.drop_index("ix_clips_gate_status", table_name="clips")
    op.drop_column("clips", "formula_score")
    op.drop_column("clips", "gate_reasons")
    op.drop_column("clips", "gate_status")
