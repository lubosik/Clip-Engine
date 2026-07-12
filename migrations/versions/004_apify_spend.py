"""Apify spend ledger — apify_runs table.

Revision ID: 004
Revises: 003
Create Date: 2026-07-12 12:00:00.000000

One row per Apify actor run with the REAL billed cost (usageTotalUsd) as
reported by the Apify API.  Powers the apify section of /api/spend and the
real-cost --max-apify-spend guard in the producer.

Downgrade note: all recorded Apify spend history is permanently lost on
downgrade; no other table is affected.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "apify_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=False, server_default="unknown"),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("campaign", sa.String(128), nullable=True),
        sa.Column(
            "kind",
            sa.String(32),
            nullable=False,
            server_default="other",
            comment="discovery | transcript | comments | analytics | other",
        ),
        sa.Column("items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cost_usd",
            sa.Float(),
            nullable=True,
            comment="usageTotalUsd reported by Apify — real billed spend",
        ),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_apify_runs_campaign", "apify_runs", ["campaign"])
    op.create_index("ix_apify_runs_created_at", "apify_runs", ["created_at"])
    op.create_index("ix_apify_runs_kind", "apify_runs", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_apify_runs_kind", table_name="apify_runs")
    op.drop_index("ix_apify_runs_created_at", table_name="apify_runs")
    op.drop_index("ix_apify_runs_campaign", table_name="apify_runs")
    op.drop_table("apify_runs")
