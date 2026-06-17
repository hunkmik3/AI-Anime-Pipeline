"""phase 9.2 budgeting: app_user budget/spent + usage_record

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("budget_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "app_user",
        sa.Column("spent_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
    )

    op.create_table(
        "usage_record",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'video'")),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("estimated_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("actual_usd", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'reserved'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], name="fk_usage_record_user"),
    )
    op.create_index("ix_usage_record_user_id", "usage_record", ["user_id"])
    op.create_index("ix_usage_record_request_id", "usage_record", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_usage_record_request_id", table_name="usage_record")
    op.drop_index("ix_usage_record_user_id", table_name="usage_record")
    op.drop_table("usage_record")
    op.drop_column("app_user", "spent_usd")
    op.drop_column("app_user", "budget_usd")
