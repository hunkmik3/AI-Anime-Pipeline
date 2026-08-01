"""reference: which image model actually produced this

The Flow Studio viewer shows a model badge on each generated image, and "which
model made this" is not answerable from the prompt: the backend can substitute a
fallback (Atrium without a public input URL falls back to the direct Gemini
engine), so the requested model and the one that ran are not always the same.
Stores the RESOLVED value.

Nullable with no backfill: rows that predate the column genuinely do not know, and
guessing would put a wrong badge on real images.

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("reference", sa.Column("model_used", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("reference", "model_used")
