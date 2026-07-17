"""self-service signup: registration table

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-17

Pending signups awaiting admin approval. Kept out of ``app_user`` so a request
carries no password and reserves no username until an admin approves it.
``email`` is indexed but NOT unique — one *pending* row per email is enforced
in the service, so a rejected applicant can apply again later.
(SQLite/bundled builds get this via SQLModel.metadata.create_all.)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "registration",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("created_username", sa.Text(), nullable=True),
    )
    op.create_index("ix_registration_email", "registration", ["email"])
    op.create_index("ix_registration_status", "registration", ["status"])
    op.create_index("ix_registration_created_at", "registration", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_registration_created_at", table_name="registration")
    op.drop_index("ix_registration_status", table_name="registration")
    op.drop_index("ix_registration_email", table_name="registration")
    op.drop_table("registration")
