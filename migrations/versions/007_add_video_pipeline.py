"""Add video pipeline columns to clips.

Revision ID: 007
Revises: 006
Create Date: 2026-07-27 00:00:00.000000

Adds three columns to the clips table to support the add-video pipeline
(docs/ADD_VIDEO_CONTRACTS.md §5):

  correction_attempts  INTEGER NOT NULL DEFAULT 0
      Incremented each time the orchestrator re-renders a clip following a
      critic correction.  Max 2 corrections → 3 render attempts total.

  critic_reports       JSONB NULL
      Append-only list of CriticReport.model_dump() — one entry per render
      attempt (0, 1, 2).  Null until the first critic run.

  judge_decision       JSONB NULL
      JudgeDecision.model_dump() from the single judge call per clip.
      Null until the judge runs (which happens exactly once, after the
      correction loop ends).

Downgrade note: drops the three columns.  The clips rows themselves are
unaffected and remain in the table — only the correction/critic/judge data is
lost.  Video pipeline clips already in the table revert to the pre-007 schema.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _jsonb() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "clips",
        sa.Column(
            "correction_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment=(
                "Number of critic-driven re-renders for this clip "
                "(max 2 corrections = 3 render attempts total)"
            ),
        ),
    )
    op.add_column(
        "clips",
        sa.Column(
            "critic_reports",
            _jsonb(),
            nullable=True,
            comment=(
                "list[CriticReport.model_dump()] — one entry per render attempt; "
                "appended by the video pipeline orchestrator"
            ),
        ),
    )
    op.add_column(
        "clips",
        sa.Column(
            "judge_decision",
            _jsonb(),
            nullable=True,
            comment=(
                "JudgeDecision.model_dump() — written exactly once per clip "
                "after the correction loop ends"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("clips", "judge_decision")
    op.drop_column("clips", "critic_reports")
    op.drop_column("clips", "correction_attempts")
