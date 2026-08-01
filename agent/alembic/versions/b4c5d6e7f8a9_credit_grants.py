"""credit grants for project/series budgets (Phase 11.1)

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-25

The BOD sets a base credit budget per Project and per Series (stored in the
existing JSONB bags — ``project.settings['credit_budget_usd']`` and
``series.production['credit_budget_usd']``, so no columns needed for those).
When a scope runs dry, generation is blocked; a PM may top it up, and every
top-up lands here with an amount and a **required reason** so overspend is
always traceable.

    effective budget = base + SUM(credit_grant.amount_usd for that scope)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "credit_grant",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.String(), nullable=False),          # project | series
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["granted_by"], ["app_user.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_credit_grant_scope", "credit_grant", ["scope"])
    op.create_index("ix_credit_grant_scope_id", "credit_grant", ["scope_id"])
    op.create_index("ix_credit_grant_granted_by", "credit_grant", ["granted_by"])
    op.create_index("ix_credit_grant_created_at", "credit_grant", ["created_at"])


def downgrade() -> None:
    op.drop_table("credit_grant")
