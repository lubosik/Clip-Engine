"""Source pipeline queue + learning loop.

Revision ID: 005
Revises: 004
Create Date: 2026-07-12 16:00:00.000000

- sources: stage / clips_identified / stage_error / stage_updated_at
  (real-time pipeline visibility; persisted so a refresh or restart never
  loses progress state)
- clips: review_feedback (structured approve/reject feedback), profile_version
- preference_profiles: versioned per-campaign preference profile distilled
  from operator decisions (in-context learning, NOT fine-tuning)

Downgrade note: drops all pipeline-stage state and all captured feedback /
preference profiles. Clips and sources themselves are unaffected.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _jsonb():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # ── sources: pipeline stage ────────────────────────────────────────────
    op.add_column(
        "sources",
        sa.Column(
            "stage",
            sa.String(32),
            nullable=False,
            server_default="queued",
            comment="queued | transcribing | identifying | rendering | reviewing | complete | failed",
        ),
    )
    op.add_column(
        "sources",
        sa.Column(
            "clips_identified",
            sa.Integer(),
            nullable=True,
            comment="clips selected by ranking — the progress denominator",
        ),
    )
    op.add_column("sources", sa.Column("stage_error", sa.Text(), nullable=True))
    op.add_column(
        "sources",
        sa.Column("stage_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sources_stage", "sources", ["stage"])

    # Backfill: everything already processed is historical.
    op.execute(
        "UPDATE sources SET stage = 'complete' WHERE status IN ('done', 'partially_done')"
    )

    # ── clips: structured review feedback ─────────────────────────────────
    op.add_column(
        "clips",
        sa.Column(
            "review_feedback",
            _jsonb(),
            nullable=True,
            comment='{"action","reasons","note","decided_at"}',
        ),
    )
    op.add_column(
        "clips",
        sa.Column(
            "profile_version",
            sa.Integer(),
            nullable=True,
            comment="preference profile version active when this clip was ranked",
        ),
    )

    # ── preference_profiles ────────────────────────────────────────────────
    op.create_table(
        "preference_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "campaign",
            sa.String(128),
            sa.ForeignKey("campaigns.name", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rules", _jsonb(), nullable=False),
        sa.Column("meta", _jsonb(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("campaign", "version", name="uq_preference_profiles_campaign_version"),
    )
    op.create_index("ix_preference_profiles_campaign", "preference_profiles", ["campaign"])


def downgrade() -> None:
    op.drop_index("ix_preference_profiles_campaign", table_name="preference_profiles")
    op.drop_table("preference_profiles")
    op.drop_column("clips", "profile_version")
    op.drop_column("clips", "review_feedback")
    op.drop_index("ix_sources_stage", table_name="sources")
    op.drop_column("sources", "stage_updated_at")
    op.drop_column("sources", "stage_error")
    op.drop_column("sources", "clips_identified")
    op.drop_column("sources", "stage")
