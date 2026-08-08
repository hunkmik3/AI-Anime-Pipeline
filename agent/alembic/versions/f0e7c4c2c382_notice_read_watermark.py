"""giantflow: how far each account has read its notifications

The only stored part of the notifications tab. The feed itself is derived from
`flow_panel_event` and the panels' own statuses at read time — see
`services/flow_notices.py` for why nothing is fanned out on write. What cannot be
derived is whether *you* have already looked, so that is one row per user and
nothing else.

Revision ID: f0e7c4c2c382
Revises: 74eb7ca587cc
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0e7c4c2c382"
down_revision: Union[str, Sequence[str], None] = "74eb7ca587cc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "flow_notice_read",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("flow_notice_read")
