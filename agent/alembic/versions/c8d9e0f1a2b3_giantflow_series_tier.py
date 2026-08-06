"""giantflow: a Project holds Series

The studio's tiers are four deep, not three. What the app has been calling a
Project is a **Series** — one comic, e.g. 26004_UL-X-MEN — and a Project sits
above them as the container for the whole slate.

    Project        the studio's body of work
      Series       one comic
        Batch      one artist's share of it
          Panel    the unit of review

So ``flow_project`` becomes ``flow_series`` and a new ``flow_project`` is created
above it. Renaming rather than adding a differently-named parent: the old table
holds comics, and leaving it named "project" while the UI calls it a series is
the kind of drift that costs an afternoon a year from now.

**Membership stays on the SERIES.** ``flow_project_member`` becomes
``flow_series_member`` unchanged in shape. Moving it up to the new Project would
mean one role for the entire studio output, which is the opposite of what
per-comic roles are for — a PM on X-MEN is not automatically a PM on MAGMEL.

Existing series are adopted by a single project so nothing is orphaned. It is
named "Global Comix" after what the studio calls the slate; renaming it is a
click.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. The comic table takes its real name, and the new top tier takes the one
    #    it was borrowing. Order matters: rename before creating.
    op.rename_table("flow_project", "flow_series")
    op.rename_table("flow_project_member", "flow_series_member")
    op.alter_column("flow_series_member", "project_id", new_column_name="series_id")

    # Postgres does NOT rename a table's indexes with it, so the old ones still
    # occupy the `ix_flow_project_*` names — and the new table wants them. Rename
    # them across too, or CREATE INDEX below collides with a leftover.
    for old, new in (
        ("ix_flow_project_created_at", "ix_flow_series_created_at"),
        ("ix_flow_project_created_by", "ix_flow_series_created_by"),
        ("ix_flow_project_order_index", "ix_flow_series_order_index"),
        ("ix_flow_project_member_project_id", "ix_flow_series_member_series_id"),
        ("ix_flow_project_member_user_id", "ix_flow_series_member_user_id"),
        ("ix_flow_batch_project_id", "ix_flow_batch_series_id"),
    ):
        op.execute(f'ALTER INDEX IF EXISTS {old} RENAME TO {new}')
    op.execute("ALTER TABLE flow_series RENAME CONSTRAINT flow_project_pkey TO flow_series_pkey")
    op.execute(
        "ALTER TABLE flow_series_member "
        "RENAME CONSTRAINT flow_project_member_pkey TO flow_series_member_pkey"
    )

    op.create_table(
        "flow_project",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("cover_media_id", sa.String(), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_flow_project_order_index", "flow_project", ["order_index"])

    # 2. Nullable first so the ALTER succeeds on existing rows, filled in, then
    #    tightened — the same shape the batch migration used.
    op.add_column("flow_series", sa.Column("project_id", sa.Integer(), nullable=True))

    conn = op.get_bind()
    has_series = conn.execute(sa.text("SELECT 1 FROM flow_series LIMIT 1")).first()
    if has_series is not None:
        project_id = conn.execute(
            sa.text(
                "INSERT INTO flow_project (name, order_index, created_at) "
                "VALUES ('Global Comix', 0, NOW()) RETURNING id"
            )
        ).scalar_one()
        conn.execute(sa.text("UPDATE flow_series SET project_id = :pid"), {"pid": project_id})

    op.alter_column("flow_series", "project_id", nullable=False)
    op.create_index("ix_flow_series_project_id", "flow_series", ["project_id"])
    op.create_foreign_key(
        "fk_flow_series_project_id", "flow_series", "flow_project",
        ["project_id"], ["id"], ondelete="CASCADE",
    )

    # 3. flow_batch pointed at the old flow_project; it now points at a series.
    op.alter_column("flow_batch", "project_id", new_column_name="series_id")


def downgrade() -> None:
    op.alter_column("flow_batch", "series_id", new_column_name="project_id")
    op.drop_constraint("fk_flow_series_project_id", "flow_series", type_="foreignkey")
    op.drop_index("ix_flow_series_project_id", table_name="flow_series")
    op.drop_column("flow_series", "project_id")
    op.drop_index("ix_flow_project_order_index", table_name="flow_project")
    op.drop_table("flow_project")
    op.alter_column("flow_series_member", "series_id", new_column_name="project_id")
    op.rename_table("flow_series_member", "flow_project_member")
    op.rename_table("flow_series", "flow_project")
