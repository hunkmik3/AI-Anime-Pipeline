"""an episode can have helpers

One owner stays one owner: `scene.assignee_user_id` is still the only person who
may hand the cut in, because a deliverable two people can submit is one nobody
is accountable for. This table is everyone else who works inside it — added when
the owner is overloaded, which a chapter split between three panel artists makes
routine.

Revision ID: 2d2c0aaf5010
Revises: ed819d8d29e1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2d2c0aaf5010"
down_revision: Union[str, Sequence[str], None] = "ed819d8d29e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scene_collaborator",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scene_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["scene_id"], ["scene.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by"], ["app_user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scene_id", "user_id", name="uq_scene_collaborator"),
    )
    op.create_index("ix_scene_collaborator_scene_id", "scene_collaborator", ["scene_id"])
    op.create_index("ix_scene_collaborator_user_id", "scene_collaborator", ["user_id"])


def downgrade() -> None:
    op.drop_table("scene_collaborator")
