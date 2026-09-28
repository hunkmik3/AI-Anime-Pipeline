"""video_analysis.options: how a video is to be read (detail level, language)

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-09-19

A resumed analysis has to read the video the same way the first pass did, so
the choice lives on the row rather than in the request that started it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f0a1b2c3d4e5"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("video_analysis"):
        return
    if any(c["name"] == "options" for c in inspector.get_columns("video_analysis")):
        return
    op.add_column(
        "video_analysis",
        sa.Column("options", _JSON, nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("video_analysis"):
        return
    if any(c["name"] == "options" for c in inspector.get_columns("video_analysis")):
        op.drop_column("video_analysis", "options")
