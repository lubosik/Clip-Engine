"""Add pipeline_events table for live progress SSE.

Revision ID: 008
Revises: 007
Create Date: 2026-07-29 00:00:00.000000

Adds the pipeline_events table (PROGRESS_EVENTS_CONTRACTS.md §1):

  id            BIGSERIAL PK          — global monotonic; used as SSE Last-Event-ID
  source_id     String(512) NOT NULL  — FK → sources.source_id ON DELETE CASCADE
  clip_id       Integer NULL
  stage         String(24) NOT NULL   — stage vocabulary per §2
  status        String(12) NOT NULL   — running | done | failed | corrected
  progress_n    Integer NULL
  progress_total Integer NULL
  detail        Text NULL             — human-readable line shown verbatim in UI
  reason        Text NULL             — failure/correction reason
  created_at    timestamptz NOT NULL

Index: (source_id, id) — needed for SSE replay and efficient per-source tailing.

Downgrade note: drops the table entirely.  All pipeline progress event history
is lost on downgrade; source/clip state rows in other tables are unaffected.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pipeline_events",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("source_id", sa.String(512), nullable=False),
        sa.Column("clip_id", sa.Integer(), nullable=True),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("progress_n", sa.Integer(), nullable=True),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.source_id"],
            name="fk_pipeline_events_source_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pipeline_events_source_id_id",
        "pipeline_events",
        ["source_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_events_source_id_id", table_name="pipeline_events")
    op.drop_table("pipeline_events")
