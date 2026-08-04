"""giantflow: batches — a project holds work packages, each with its own import

The structure the studio actually uses is two tiers, not one:

    Project   the comic — a name, nothing else
      Batch   one artist's share: its own name, its own uploaded folder of panels
        Panel reviewed individually, as before

Material arrives already divided by who is doing it, so a batch owns the import
and there is no range-splitting step — there is never one big pile to split.

The assignee moves from the panel to the batch. Two copies of "who is doing this"
is one fact stored twice, and they drift.

Existing panels are not orphaned: every project with panels gets a default batch
("Batch 1") that adopts them, so an import done before this migration survives
with its order intact.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "flow_batch",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("flow_project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("assignee_user_id", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_flow_batch_project_id", "flow_batch", ["project_id"])
    op.create_index("ix_flow_batch_assignee_user_id", "flow_batch", ["assignee_user_id"])
    op.create_index("ix_flow_batch_order_index", "flow_batch", ["order_index"])
    op.create_index("ix_flow_batch_created_at", "flow_batch", ["created_at"])

    # Nullable first so existing rows survive the ALTER; filled in below, then
    # tightened. Doing it in one step would fail on any project that has panels.
    op.add_column("flow_panel", sa.Column("batch_id", sa.Integer(), nullable=True))

    conn = op.get_bind()
    projects = conn.execute(
        sa.text("SELECT DISTINCT project_id FROM flow_panel")
    ).fetchall()
    for (project_id,) in projects:
        batch_id = conn.execute(
            sa.text(
                "INSERT INTO flow_batch (project_id, name, order_index, created_at) "
                "VALUES (:pid, 'Batch 1', 0, NOW()) RETURNING id"
            ),
            {"pid": project_id},
        ).scalar_one()
        conn.execute(
            sa.text("UPDATE flow_panel SET batch_id = :bid WHERE project_id = :pid"),
            {"bid": batch_id, "pid": project_id},
        )

    op.alter_column("flow_panel", "batch_id", nullable=False)
    op.create_index("ix_flow_panel_batch_id", "flow_panel", ["batch_id"])
    op.create_foreign_key(
        "fk_flow_panel_batch_id", "flow_panel", "flow_batch",
        ["batch_id"], ["id"], ondelete="CASCADE",
    )

    # Panel codes are unique within a BATCH now: two artists' folders can each
    # legitimately contain a PANEL001.
    op.drop_constraint("uq_flow_panel_code", "flow_panel", type_="unique")
    op.create_unique_constraint("uq_flow_panel_code", "flow_panel", ["batch_id", "code"])

    op.drop_index("ix_flow_panel_assignee_user_id", table_name="flow_panel")
    op.drop_column("flow_panel", "assignee_user_id")
    op.drop_index("ix_flow_panel_project_id", table_name="flow_panel")
    op.drop_column("flow_panel", "project_id")


def downgrade() -> None:
    op.add_column("flow_panel", sa.Column("project_id", sa.Integer(), nullable=True))
    op.add_column("flow_panel", sa.Column("assignee_user_id", sa.Uuid(), nullable=True))
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE flow_panel SET project_id = b.project_id "
            "FROM flow_batch b WHERE b.id = flow_panel.batch_id"
        )
    )
    op.drop_constraint("uq_flow_panel_code", "flow_panel", type_="unique")
    op.create_unique_constraint("uq_flow_panel_code", "flow_panel", ["project_id", "code"])
    op.drop_constraint("fk_flow_panel_batch_id", "flow_panel", type_="foreignkey")
    op.drop_index("ix_flow_panel_batch_id", table_name="flow_panel")
    op.drop_column("flow_panel", "batch_id")
    op.drop_table("flow_batch")
