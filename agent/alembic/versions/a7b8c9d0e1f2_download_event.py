"""download_event — track which outputs users actually downloaded

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-14

A download is the strongest "this output was used" signal available without
asking users to click anything extra. The media route doubles as the preview
route, so downloads are recorded by an explicit ping from the download button.
(SQLite/bundled builds get this via SQLModel.metadata.create_all.)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "download_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("media_id", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_download_event_user_id", "download_event", ["user_id"])
    op.create_index("ix_download_event_media_id", "download_event", ["media_id"])
    op.create_index("ix_download_event_node_id", "download_event", ["node_id"])
    op.create_index("ix_download_event_created_at", "download_event", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_download_event_created_at", table_name="download_event")
    op.drop_index("ix_download_event_node_id", table_name="download_event")
    op.drop_index("ix_download_event_media_id", table_name="download_event")
    op.drop_index("ix_download_event_user_id", table_name="download_event")
    op.drop_table("download_event")
