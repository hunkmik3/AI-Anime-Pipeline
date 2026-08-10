"""flow_project.studio_project_id — the slate's production counterpart

Every comic now gets its Giant Studio counterpart the moment it is created, so
the slate needs somewhere to remember which production project it hands over
into. Stored, not matched by name: names get edited, and a rename would otherwise
quietly start a second project beside the first.

ON DELETE SET NULL, matching the other cross-product links: deleting a production
project must not take the comic slate with it, and an unlinked slate is a state a
PM can see and fix.

Revision ID: c4e1a9f6b2d8
Revises: b3f7c0d19e45
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4e1a9f6b2d8"
down_revision = "b3f7c0d19e45"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("flow_project")}
    if "studio_project_id" in cols:
        return
    with op.batch_alter_table("flow_project") as b:
        b.add_column(sa.Column("studio_project_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_flow_project_studio_project_id", "flow_project", ["studio_project_id"]
    )
    if bind.dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_flow_project_studio_project",
            "flow_project",
            "project",
            ["studio_project_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.drop_constraint(
            "fk_flow_project_studio_project", "flow_project", type_="foreignkey"
        )
    op.drop_index("ix_flow_project_studio_project_id", table_name="flow_project")
    with op.batch_alter_table("flow_project") as b:
        b.drop_column("studio_project_id")
