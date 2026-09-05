"""flow_panel.assignee_user_id — per-panel handover

Restores the column that ``d2e3f4a5b6c7_giantflow_batches`` dropped when
assignment moved up to the batch. Per-panel transfer brought the narrower
case back: a batch has one assignee, and setting this hands THIS panel to
someone else without splitting the batch.

The model (FlowPanel.assignee_user_id) already declares it, so on any
database migrated past d2e3f4a5b6c7 every panel query fails with
``UndefinedColumn: column flow_panel.assignee_user_id does not exist``.
Nullable, no backfill: null means "follow the batch", which is the state
every existing row should be in.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded, because the column's absence is not certain. A database that
    # never ran d2e3f4a5b6c7, or one where somebody added the column by hand to
    # get the panel screens working again, already has it — and an unguarded
    # add_column would fail the whole deploy on the very machines that were
    # already coping. Same reason the index is checked separately: the two can
    # be out of step if it was added by hand.
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("flow_panel")}
    if "assignee_user_id" not in cols:
        with op.batch_alter_table("flow_panel") as b:
            b.add_column(sa.Column("assignee_user_id", sa.Uuid(), nullable=True))
    idx = {i["name"] for i in insp.get_indexes("flow_panel")}
    if "ix_flow_panel_assignee_user_id" not in idx:
        op.create_index(
            "ix_flow_panel_assignee_user_id", "flow_panel", ["assignee_user_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "ix_flow_panel_assignee_user_id" in {i["name"] for i in insp.get_indexes("flow_panel")}:
        op.drop_index("ix_flow_panel_assignee_user_id", table_name="flow_panel")
    if "assignee_user_id" in {c["name"] for c in insp.get_columns("flow_panel")}:
        with op.batch_alter_table("flow_panel") as b:
            b.drop_column("assignee_user_id")
