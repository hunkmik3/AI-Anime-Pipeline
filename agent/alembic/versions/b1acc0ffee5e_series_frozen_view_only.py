"""series.frozen — view-only archive lock

A frozen series is read-only: episodes/sequences under it can't be edited or
generated into. Enforced in permissions.require_scene.

Revision ID: b1acc0ffee5e
Revises: c4e1a9f6b2d8
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa

revision = "b1acc0ffee5e"
down_revision = "f7d3a91c2e58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "series",
        sa.Column("frozen", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_series_frozen", "series", ["frozen"])


def downgrade() -> None:
    op.drop_index("ix_series_frozen", "series")
    op.drop_column("series", "frozen")
