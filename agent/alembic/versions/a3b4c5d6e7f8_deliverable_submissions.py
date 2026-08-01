"""deliverable submissions + review (Phase 11)

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-07-25

An Episode is the deliverable: its sequences are generated in-app, the cut is
edited OUTSIDE the app, then a Drive link is submitted for review.

- ``series.producer_user_id`` — the Series Producer, first link in the approver
  chain (nullable → review falls through to the project's PM).
- ``scene.assignee_user_id`` — the employee who owns the episode and is the only
  one who may submit it.
- ``scene.deliverable_status`` — draft | submitted | approved | paid.
- ``submission`` — append-only history of delivery attempts (v1, v2, …), each
  with its Drive link, resolved approver and verdict + reason.

Additive only; existing rows default to ``draft`` with no assignee.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Series Producer ───────────────────────────────────────────────────
    op.add_column(
        "series",
        sa.Column("producer_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_series_producer_user",
        "series",
        "app_user",
        ["producer_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_series_producer_user_id", "series", ["producer_user_id"])

    # ── Episode: assignee + deliverable lifecycle ─────────────────────────
    op.add_column(
        "scene",
        sa.Column("assignee_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_scene_assignee_user",
        "scene",
        "app_user",
        ["assignee_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_scene_assignee_user_id", "scene", ["assignee_user_id"])
    op.add_column(
        "scene",
        sa.Column(
            "deliverable_status",
            sa.String(),
            nullable=False,
            server_default="draft",
        ),
    )
    op.create_index("ix_scene_deliverable_status", "scene", ["deliverable_status"])

    # ── Submission history ────────────────────────────────────────────────
    op.create_table(
        "submission",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scene_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("drive_url", sa.String(), nullable=False, server_default=""),
        sa.Column("drive_file_id", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("submitted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="submitted"),
        sa.Column("approver_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["scene_id"], ["scene.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["submitted_by"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approver_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["app_user.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_submission_scene_id", "submission", ["scene_id"])
    op.create_index("ix_submission_status", "submission", ["status"])
    op.create_index("ix_submission_submitted_by", "submission", ["submitted_by"])
    op.create_index("ix_submission_submitted_at", "submission", ["submitted_at"])
    op.create_index("ix_submission_approver_user_id", "submission", ["approver_user_id"])


def downgrade() -> None:
    op.drop_table("submission")
    op.drop_index("ix_scene_deliverable_status", table_name="scene")
    op.drop_column("scene", "deliverable_status")
    op.drop_index("ix_scene_assignee_user_id", table_name="scene")
    op.drop_constraint("fk_scene_assignee_user", "scene", type_="foreignkey")
    op.drop_column("scene", "assignee_user_id")
    op.drop_index("ix_series_producer_user_id", table_name="series")
    op.drop_constraint("fk_series_producer_user", "series", type_="foreignkey")
    op.drop_column("series", "producer_user_id")
