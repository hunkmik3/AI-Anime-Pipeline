"""Flow Studio: its own board list, and one nullable link from Reference

Flow Studio (``/giantflow``) was brought over from the manga_extract repo as a
standalone surface — it is deliberately NOT wired into Project → Series →
Episode, per-project RBAC, or the credit budgets yet.

It still needs somewhere to group its generated images, which is what
``flow_board`` is: the studio's own project list. Its images go into the existing
``reference`` table — not a parallel one — scoped by ``source_board_id``. That
choice is the point: when the studio is later folded into the hierarchy, the work
is backfilling ``reference.project_id`` from the board mapping, not migrating
image data out of a second table.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "flow_board",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_flow_board_created_at", "flow_board", ["created_at"])

    op.add_column("reference", sa.Column("source_board_id", sa.Integer(), nullable=True))
    op.create_index("ix_reference_source_board_id", "reference", ["source_board_id"])
    op.create_foreign_key(
        "fk_reference_source_board_id",
        "reference",
        "flow_board",
        ["source_board_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_reference_source_board_id", "reference", type_="foreignkey")
    op.drop_index("ix_reference_source_board_id", table_name="reference")
    op.drop_column("reference", "source_board_id")
    op.drop_index("ix_flow_board_created_at", table_name="flow_board")
    op.drop_table("flow_board")
