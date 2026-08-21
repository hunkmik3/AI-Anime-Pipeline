"""app_user HR / employee-directory fields

Adds the staff-spreadsheet columns to app_user:
  employee_code, staff_category, job_title, rank, employment_status.

All informational except employment_status (default 'active'), whose
resigned/terminated values the app also mirrors onto the auth status
(login block). See flowboard.services.user_service.set_employment_status.

Revision ID: d1e2f3a4b5c6
Revises: c7f10ade0b1a
Create Date: 2026-08-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c7f10ade0b1a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("employee_code", sa.String(), nullable=True))
    op.add_column("app_user", sa.Column("staff_category", sa.String(), nullable=True))
    op.add_column("app_user", sa.Column("job_title", sa.String(), nullable=True))
    op.add_column("app_user", sa.Column("rank", sa.String(), nullable=True))
    op.add_column(
        "app_user",
        sa.Column(
            "employment_status",
            sa.String(),
            nullable=False,
            server_default="active",
        ),
    )
    op.create_index("ix_app_user_employee_code", "app_user", ["employee_code"])


def downgrade() -> None:
    op.drop_index("ix_app_user_employee_code", table_name="app_user")
    op.drop_column("app_user", "employment_status")
    op.drop_column("app_user", "rank")
    op.drop_column("app_user", "job_title")
    op.drop_column("app_user", "staff_category")
    op.drop_column("app_user", "employee_code")
