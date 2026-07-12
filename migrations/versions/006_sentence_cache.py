"""Sentence cache on transcripts.

Revision ID: 006
Revises: 005
Create Date: 2026-07-12 18:00:00.000000

Adds:
  - transcripts.sentences  JSONB NULL
      Punctuation-restored sentence spans [{"text","start","end"}] produced
      by core.punctuate.restore_sentences().  Cached here so the
      PunctCapSegModelONNX runs at most ONCE per source across all runs.
      NULL means either not yet computed or the model was unavailable (the
      pipeline falls back to the regex path in core.sentences.build_sentence_spans).

Downgrade note: drops the cached sentences column.  The pipeline re-computes
spans on demand from the stored segments on the next run — no data loss.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _jsonb() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "transcripts",
        sa.Column(
            "sentences",
            _jsonb(),
            nullable=True,
            comment=(
                '[{"text","start","end"}] punctuation-restored sentence spans '
                "(core.punctuate.restore_sentences); NULL = not yet computed or model unavailable"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("transcripts", "sentences")
