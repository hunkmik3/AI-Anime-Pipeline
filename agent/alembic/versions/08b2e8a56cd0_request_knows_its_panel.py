"""a generation records the panel it was for

Spend is attributed by walking `request.node_id → shot → scene → project`.
Giantflow panels are not on that tree, so a panel generation could not be
attributed at all — the panel id went into `params["__panel_id"]`, which is true
and unjoinable, and every one of those runs appeared in the ledger as money
belonging to nobody.

Backfills from that JSON key where the panel still exists. Rows whose panel is
gone, and the ones that never carried the key, keep a NULL — they are genuinely
unattributable and saying so is better than guessing.

Revision ID: 08b2e8a56cd0
Revises: aadf1ba7a6c5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "08b2e8a56cd0"
down_revision: Union[str, Sequence[str], None] = "aadf1ba7a6c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("request", sa.Column("flow_panel_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_request_flow_panel", "request", "flow_panel", ["flow_panel_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_request_flow_panel_id", "request", ["flow_panel_id"])

    # Only where the panel is still there — the FK would reject the rest, and a
    # request pointing at a deleted panel is not information anyway.
    n = op.get_bind().execute(sa.text("""
        UPDATE request r
        SET flow_panel_id = (r.params ->> '__panel_id')::int
        WHERE r.node_id IS NULL
          AND r.params ? '__panel_id'
          AND EXISTS (
              SELECT 1 FROM flow_panel p
              WHERE p.id = (r.params ->> '__panel_id')::int
          )
    """)).rowcount
    if n:
        print(f"  backfilled {n} request(s) from params.__panel_id")


def downgrade() -> None:
    op.drop_index("ix_request_flow_panel_id", table_name="request")
    op.drop_constraint("fk_request_flow_panel", "request", type_="foreignkey")
    op.drop_column("request", "flow_panel_id")
