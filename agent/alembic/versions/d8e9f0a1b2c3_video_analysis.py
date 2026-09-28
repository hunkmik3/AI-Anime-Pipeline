"""video_analysis: reference videos broken down shot by shot

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-09-14

Analysis and adaptation are separate JSON columns — see the model docstring.
Guarded with ``sa.inspect`` like the migration before it, for the desktop
build that creates its schema with ``create_all``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d8e9f0a1b2c3"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("video_analysis"):
        return

    op.create_table(
        "video_analysis",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("automation_project_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("progress", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("analysis", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("adaptation", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("adaptation_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["app_user.id"]),
        # A board being deleted must not take its analysed videos with it.
        sa.ForeignKeyConstraint(["automation_project_id"], ["automation_project.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_video_analysis_owner_user_id", "video_analysis", ["owner_user_id"])
    op.create_index("ix_video_analysis_automation_project_id", "video_analysis", ["automation_project_id"])
    op.create_index("ix_video_analysis_updated_at", "video_analysis", ["updated_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("video_analysis"):
        return
    op.drop_index("ix_video_analysis_updated_at", table_name="video_analysis")
    op.drop_index("ix_video_analysis_automation_project_id", table_name="video_analysis")
    op.drop_index("ix_video_analysis_owner_user_id", table_name="video_analysis")
    op.drop_table("video_analysis")
