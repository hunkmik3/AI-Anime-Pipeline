"""automation_project: boards for the /automation surface

Revision ID: c7d8e9f0a1b2
Revises: f3a4b5c6d7e8
Create Date: 2026-09-10

One table, one JSON column for the board. See the model docstring for why the
node graph is not modelled as tables of its own while its shape is moving.

Guarded with ``sa.inspect`` the same way the two migrations before it are: the
desktop build creates its schema with ``SQLModel.metadata.create_all``, so on
that path the table is already there and this must no-op rather than blow up.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c7d8e9f0a1b2"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("automation_project"):
        return

    op.create_table(
        "automation_project",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("script", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("logline", sa.String(), nullable=False, server_default=""),
        sa.Column("runtime_seconds", sa.Integer(), nullable=True),
        sa.Column("board", _JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["app_user.id"]),
    )
    op.create_index(
        "ix_automation_project_owner_user_id", "automation_project", ["owner_user_id"]
    )
    # The list is ordered by most-recently-touched, which is the only query
    # this table serves besides fetching one board by id.
    op.create_index(
        "ix_automation_project_updated_at", "automation_project", ["updated_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("automation_project"):
        return
    op.drop_index("ix_automation_project_updated_at", table_name="automation_project")
    op.drop_index("ix_automation_project_owner_user_id", table_name="automation_project")
    op.drop_table("automation_project")
