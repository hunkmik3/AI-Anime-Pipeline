"""phase 0 security: app_user token_version + login lockout + last_login

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-14

token_version drives stateless-token revocation (bumped on suspend /
password-change / "log out everywhere"). failed_attempts + locked_until back
the login brute-force lockout. last_login is for audit / compromise detection.
(SQLite/bundled builds get these via SQLModel.metadata.create_all instead.)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "app_user",
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "app_user",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "app_user",
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_user", "last_login")
    op.drop_column("app_user", "locked_until")
    op.drop_column("app_user", "failed_attempts")
    op.drop_column("app_user", "token_version")
