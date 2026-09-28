"""video_analysis.cast: the cast and places bible, with its generated sheets

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-09-18

Separate from ``adaptation`` on purpose: re-telling the film in another world
rewrites every shot, but the cast sheets and environment plates already
generated must survive that — they cost real money to make.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("video_analysis"):
        return
    if any(c["name"] == "cast" for c in inspector.get_columns("video_analysis")):
        return
    op.add_column(
        "video_analysis",
        sa.Column("cast", _JSON, nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("video_analysis"):
        return
    if any(c["name"] == "cast" for c in inspector.get_columns("video_analysis")):
        op.drop_column("video_analysis", "cast")
