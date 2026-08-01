"""credit grants need admin approval (Phase 11.2)

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-07-25

Topping up a budget is a BOD decision, not a PM's. A grant is now a *request*:
a PM asks (amount + reason) and it sits ``pending``, changing nothing, until an
admin approves it. Only approved grants count toward the effective budget.

Rows written before this step were immediately effective, so they are backfilled
to ``approved`` — the numbers people already see stay the same.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "credit_grant",
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
    )
    op.add_column(
        "credit_grant",
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "credit_grant", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("credit_grant", sa.Column("decision_note", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_credit_grant_decided_by",
        "credit_grant",
        "app_user",
        ["decided_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_credit_grant_status", "credit_grant", ["status"])
    # Pre-approval rows took effect the moment they were written — keep them so.
    op.execute("UPDATE credit_grant SET status = 'approved' WHERE status = 'pending'")


def downgrade() -> None:
    op.drop_index("ix_credit_grant_status", table_name="credit_grant")
    op.drop_constraint("fk_credit_grant_decided_by", "credit_grant", type_="foreignkey")
    op.drop_column("credit_grant", "decision_note")
    op.drop_column("credit_grant", "decided_at")
    op.drop_column("credit_grant", "decided_by")
    op.drop_column("credit_grant", "status")
