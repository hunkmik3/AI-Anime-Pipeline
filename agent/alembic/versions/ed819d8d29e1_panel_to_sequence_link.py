"""a comic delivers into a production series

Three columns and no data change. `flow_series.studio_series_id` is the one a
person sets — which production Series this comic hands over to. The other two
are records of what already happened: which Episode a chapter became, and which
Sequence a panel became. The second exists so approving a panel twice — which a
reopen-and-re-approve does — hands over once.

All nullable. A comic that is not linked does not deliver, which is the right
answer for one being adapted for print.

Revision ID: ed819d8d29e1
Revises: 05289da5c4a9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ed819d8d29e1"
down_revision: Union[str, Sequence[str], None] = "05289da5c4a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COLUMNS = [
    ("flow_series", "studio_series_id", "series"),
    ("flow_chapter", "studio_scene_id", "scene"),
    ("flow_panel", "studio_shot_id", "shot"),
]


def upgrade() -> None:
    for table, col, target in COLUMNS:
        op.add_column(table, sa.Column(col, sa.Uuid(), nullable=True))
        # SET NULL, not the default NO ACTION. These columns point ACROSS a
        # product boundary at rows the other side owns and may delete: a PM
        # removing a production series must not get a foreign-key 500 from a
        # comic they have never heard of. Nulling is also exactly what the
        # delivery service already expects to find — it treats a stale pointer
        # as "not delivered yet" and hands the panel over again.
        op.create_foreign_key(
            f"fk_{table}_{col}", table, target, [col], ["id"], ondelete="SET NULL"
        )
        op.create_index(f"ix_{table}_{col}", table, [col])


def downgrade() -> None:
    for table, col, _ in reversed(COLUMNS):
        op.drop_index(f"ix_{table}_{col}", table_name=table)
        op.drop_constraint(f"fk_{table}_{col}", table, type_="foreignkey")
        op.drop_column(table, col)
