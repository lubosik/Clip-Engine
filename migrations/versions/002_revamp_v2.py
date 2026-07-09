"""Revamp v2 — clips kind/mode/aspect/meme_meta, nullable source/start/end,
render_jobs and meme_profiles tables.

Revision ID: 002
Revises: 001
Create Date: 2026-07-09 00:00:00.000000

Downgrade note: reverting source_id / start / end to NOT NULL will fail if any
meme rows have NULL values in those columns.  The downgrade attempts a best-
effort SET before altering, but production meme data must be removed first.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # clips — add new columns                                              #
    # ------------------------------------------------------------------ #
    op.add_column(
        "clips",
        sa.Column("kind", sa.String(8), nullable=False, server_default="clip"),
    )
    op.add_column(
        "clips",
        sa.Column("mode", sa.String(12), nullable=False, server_default="production"),
    )
    op.add_column(
        "clips",
        sa.Column("aspect", sa.String(8), nullable=False, server_default="9:16"),
    )
    op.add_column(
        "clips",
        sa.Column(
            "meme_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # clips — make source_id, start, end nullable (memes have no source video)
    # In Postgres this only changes the NOT NULL constraint; FK constraint is
    # unaffected — a NULL FK value is always valid.
    op.alter_column("clips", "source_id", existing_type=sa.String(512), nullable=True)
    op.alter_column("clips", "start", existing_type=sa.Float(), nullable=True)
    op.alter_column("clips", "end", existing_type=sa.Float(), nullable=True)

    # index on kind for Clips/Memes filter
    op.create_index("ix_clips_kind", "clips", ["kind"])

    # ------------------------------------------------------------------ #
    # render_jobs — Modal spend ledger                                     #
    # ------------------------------------------------------------------ #
    op.create_table(
        "render_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("clip_id", sa.Integer(), nullable=True),
        sa.Column("campaign", sa.String(128), nullable=False),
        sa.Column("backend", sa.String(32), nullable=False),
        sa.Column("gpu", sa.String(64), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=False, server_default="0"),
        sa.Column("rate_per_s", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cost_estimate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["clip_id"], ["clips.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_render_jobs_campaign", "render_jobs", ["campaign"])
    op.create_index("ix_render_jobs_created_at", "render_jobs", ["created_at"])

    # ------------------------------------------------------------------ #
    # meme_profiles — versioned meme style profiles                        #
    # ------------------------------------------------------------------ #
    op.create_table(
        "meme_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("campaign", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign", "version", name="uq_meme_profiles_campaign_version"),
    )
    op.create_index("ix_meme_profiles_campaign", "meme_profiles", ["campaign"])


def downgrade() -> None:
    # ------------------------------------------------------------------ #
    # meme_profiles                                                         #
    # ------------------------------------------------------------------ #
    op.drop_index("ix_meme_profiles_campaign", table_name="meme_profiles")
    op.drop_table("meme_profiles")

    # ------------------------------------------------------------------ #
    # render_jobs                                                           #
    # ------------------------------------------------------------------ #
    op.drop_index("ix_render_jobs_created_at", table_name="render_jobs")
    op.drop_index("ix_render_jobs_campaign", table_name="render_jobs")
    op.drop_table("render_jobs")

    # ------------------------------------------------------------------ #
    # clips — revert nullable columns and drop new columns                 #
    # ------------------------------------------------------------------ #
    # WARNING: setting NOT NULL will fail if any meme rows have NULL values.
    # Best-effort: set a placeholder for NULL source_id rows before altering.
    op.execute("UPDATE clips SET source_id = 'deleted:0' WHERE source_id IS NULL")
    op.execute("UPDATE clips SET start = 0.0 WHERE start IS NULL")
    op.execute("UPDATE clips SET end = 0.0 WHERE end IS NULL")

    op.alter_column("clips", "source_id", existing_type=sa.String(512), nullable=False)
    op.alter_column("clips", "start", existing_type=sa.Float(), nullable=False)
    op.alter_column("clips", "end", existing_type=sa.Float(), nullable=False)

    op.drop_index("ix_clips_kind", table_name="clips")
    op.drop_column("clips", "meme_meta")
    op.drop_column("clips", "aspect")
    op.drop_column("clips", "mode")
    op.drop_column("clips", "kind")
