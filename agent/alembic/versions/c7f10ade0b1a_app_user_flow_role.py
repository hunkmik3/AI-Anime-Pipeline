"""app_user.flow_role — default Giantflow role designation

A per-account default GF role ("this person is a GF artist"), independent of any
comic — so a team can be marked before comics exist, and the batch-assignee
picker can offer just the GF people.

Revision ID: c7f10ade0b1a
Revises: b1acc0ffee5e
Create Date: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa

revision = "c7f10ade0b1a"
down_revision = "b1acc0ffee5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("flow_role", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_user", "flow_role")
