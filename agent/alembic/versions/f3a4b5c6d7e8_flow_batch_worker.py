"""flow_batch_worker — extra people sharing a batch

A batch keeps its one `assignee` (owner/lead), and this table adds the others
who may work it alongside them: several artists splitting one pile, or a PM
stepping in. `FlowBatchWorker` was added to the model without a migration, so
on any real database the notices count — and every screen that resolves what a
person may open — dies with

    UndefinedTable: relation "flow_batch_worker" does not exist

which is a 500 on /api/flowstudio/notices/count and on the Giantflow projects
page that calls it.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-09-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded like its sibling e2f3a4b5c6d7: the machine serving these screens
    # today may already have the table by hand. An unguarded create_table would
    # fail the deploy on exactly the databases that were coping.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "flow_batch_worker" in insp.get_table_names():
        return

    op.create_table(
        "flow_batch_worker",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["flow_batch.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        # One row per person per batch — the app treats a second row as the same
        # grant, and a duplicate would double every membership count.
        sa.UniqueConstraint("batch_id", "user_id", name="uq_flow_batch_worker"),
    )
    op.create_index("ix_flow_batch_worker_batch_id", "flow_batch_worker", ["batch_id"])
    op.create_index("ix_flow_batch_worker_user_id", "flow_batch_worker", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "flow_batch_worker" not in insp.get_table_names():
        return
    op.drop_index("ix_flow_batch_worker_user_id", table_name="flow_batch_worker")
    op.drop_index("ix_flow_batch_worker_batch_id", table_name="flow_batch_worker")
    op.drop_table("flow_batch_worker")
