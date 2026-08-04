"""flow_project: hand-arranged order

Tiles were listed newest-first. A creation date does not know which show is
active right now, so the grid is arranged by hand and remembers it.

Existing rows are numbered in their current (newest-first) order, so nothing
visibly moves the first time this runs.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "flow_project",
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_flow_project_order_index", "flow_project", ["order_index"])
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id FROM flow_project ORDER BY created_at DESC, id DESC")
    ).fetchall()
    for i, (pid,) in enumerate(rows):
        conn.execute(
            sa.text("UPDATE flow_project SET order_index = :i WHERE id = :id"),
            {"i": i, "id": pid},
        )


def downgrade() -> None:
    op.drop_index("ix_flow_project_order_index", table_name="flow_project")
    op.drop_column("flow_project", "order_index")
