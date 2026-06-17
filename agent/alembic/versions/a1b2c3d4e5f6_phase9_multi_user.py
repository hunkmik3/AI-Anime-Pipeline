"""phase 9 multi-user: app_user + project.owner_user_id

Adds the app account table (admin-provisioned login) and an owner FK on
project so data is isolated per user. ``user`` is reserved in Postgres so the
table is ``app_user``.

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-06-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'user'")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_app_user_username", "app_user", ["username"], unique=True)

    op.add_column(
        "project",
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_project_owner_user_id", "project", ["owner_user_id"])
    op.create_foreign_key(
        "fk_project_owner_user", "project", "app_user", ["owner_user_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_project_owner_user", "project", type_="foreignkey")
    op.drop_index("ix_project_owner_user_id", table_name="project")
    op.drop_column("project", "owner_user_id")
    op.drop_index("ix_app_user_username", table_name="app_user")
    op.drop_table("app_user")
