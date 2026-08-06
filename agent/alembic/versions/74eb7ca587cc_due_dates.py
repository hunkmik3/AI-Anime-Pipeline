"""giantflow: who made it and when it is due

A series card said what it contained and how far along it was, but not who set it
up, when, or when it has to ship. On a slate of six comics run by several PMs
those are the first three questions asked about any of them, and the answer lived
nowhere.

``created_at`` already existed on both tiers and was simply never shown.
``created_by`` existed on series and not on chapters. ``due_date`` existed
nowhere.

A DATE, not a timestamp: a deadline is a day, and storing 23:59:59 in whichever
timezone the server happens to run in would make "due today" wrong for half the
studio.

Revision ID: 74eb7ca587cc
Revises: e0f1a2b3c4d5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "74eb7ca587cc"
down_revision: Union[str, Sequence[str], None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("flow_series", sa.Column("due_date", sa.Date(), nullable=True))
    op.add_column("flow_chapter", sa.Column("due_date", sa.Date(), nullable=True))
    op.add_column(
        "flow_chapter",
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
    )
    op.create_index("ix_flow_chapter_created_by", "flow_chapter", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_flow_chapter_created_by", table_name="flow_chapter")
    op.drop_column("flow_chapter", "created_by")
    op.drop_column("flow_chapter", "due_date")
    op.drop_column("flow_series", "due_date")
