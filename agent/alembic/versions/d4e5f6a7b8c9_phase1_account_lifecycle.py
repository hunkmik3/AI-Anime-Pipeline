"""phase 1 account lifecycle: app_user email + must_change_password

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-14

email is an optional identifier (and the link key for Google SSO in Phase 2);
must_change_password forces a password change on first login for
admin-provisioned temp passwords. (SQLite/bundled builds get these via
SQLModel.metadata.create_all.)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("email", sa.Text(), nullable=True))
    op.add_column(
        "app_user",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index("ix_app_user_email", "app_user", ["email"])


def downgrade() -> None:
    op.drop_index("ix_app_user_email", table_name="app_user")
    op.drop_column("app_user", "must_change_password")
    op.drop_column("app_user", "email")
