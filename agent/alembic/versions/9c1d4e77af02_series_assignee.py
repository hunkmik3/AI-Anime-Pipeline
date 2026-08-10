"""series.assignee_user_id — the person who BUILDS the series

A PM could hand over one episode at a time, or set `producer_user_id`. The second
looks like the answer and is not: that field is the first link in the approver
chain, so an artist put there reviews their own submissions.

So a second column, and not a rename of the first. They are the two ends of the
same handover and a series needs both filled in at once.

ON DELETE SET NULL, matching `producer_user_id`: deleting an account must not take
the series with it, and a series with nobody on it is a real state a PM can see and
fix.

Revision ID: 9c1d4e77af02
Revises: 7633d73d4abc
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c1d4e77af02"
down_revision = "7633d73d4abc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("series")}
    if "assignee_user_id" in cols:
        return
    with op.batch_alter_table("series") as b:
        b.add_column(sa.Column("assignee_user_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_series_assignee_user_id", "series", ["assignee_user_id"], unique=False
    )
    # SQLite (the desktop build) cannot add a constraint after the fact; the column
    # and index are what the queries need, and that build has one account anyway.
    if bind.dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_series_assignee_user",
            "series",
            "app_user",
            ["assignee_user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.drop_constraint("fk_series_assignee_user", "series", type_="foreignkey")
    op.drop_index("ix_series_assignee_user_id", table_name="series")
    with op.batch_alter_table("series") as b:
        b.drop_column("assignee_user_id")
