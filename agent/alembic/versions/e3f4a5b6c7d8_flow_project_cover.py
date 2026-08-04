"""flow_project: a cover image

Giantflow project cards become 9:16 tiles like the episode cards, so they need
something to show. A hand-picked cover wins; without one the card falls back to
the first panel of the first batch, which is free and usually right.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("flow_project", sa.Column("cover_media_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("flow_project", "cover_media_id")
