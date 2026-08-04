"""giantflow: panel production — projects, panels, images, notes, members

Moves the comic-adaptation pipeline into the app. The studio's PM review lived on
a Miro board (one row per panel, one column per stage) while generation happened
in giantflow; these tables are that board, with generation attached rather than
alongside.

The PANEL is the unit of work — it is what gets assigned, carries a status, is
what a note is about, and what gets exported. Panels never reference each other,
which is exactly why this is a grid and not a node graph.

Additive on purpose: ``flow_board`` and ``reference.source_board_id`` are left
alone so the existing /giantflow keeps working while the new surface is built.
They come out in the phase that replaces the UI.

Revision ID: c1d2e3f4a5b6
Revises: a9b0c1d2e3f4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "flow_project",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_flow_project_created_at", "flow_project", ["created_at"])
    op.create_index("ix_flow_project_created_by", "flow_project", ["created_by"])

    # Membership lives here rather than on project_member so a giantflow role
    # grants nothing in the production hierarchy, and vice versa.
    op.create_table(
        "flow_project_member",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("flow_project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="artist"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "user_id", name="uq_flow_project_member"),
    )
    op.create_index("ix_flow_project_member_project_id", "flow_project_member", ["project_id"])
    op.create_index("ix_flow_project_member_user_id", "flow_project_member", ["user_id"])

    op.create_table(
        "flow_panel",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("flow_project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The cutter's own name ("PANEL006"), shown as-is so it matches their
        # sheet and the Miro history this replaces.
        sa.Column("code", sa.String(), nullable=False),
        # Filename order from the import. Never re-derived: they sorted the
        # folder deliberately.
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assignee_user_id", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="todo"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "code", name="uq_flow_panel_code"),
    )
    op.create_index("ix_flow_panel_project_id", "flow_panel", ["project_id"])
    op.create_index("ix_flow_panel_code", "flow_panel", ["code"])
    op.create_index("ix_flow_panel_order_index", "flow_panel", ["order_index"])
    op.create_index("ix_flow_panel_status", "flow_panel", ["status"])
    op.create_index("ix_flow_panel_assignee_user_id", "flow_panel", ["assignee_user_id"])

    # Raw material and results share a table because they are the same thing to
    # the UI (a media id to show) and the pairing is the point — every PM note
    # compares result against original. A panel has several of each: one Miro row
    # carried three raw pieces, and every review round adds a version.
    op.create_table(
        "flow_panel_image",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "panel_id",
            sa.Integer(),
            sa.ForeignKey("flow_panel.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(), nullable=False),  # raw | generated
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("media_id", sa.String(), nullable=False),
        sa.Column("model_used", sa.String(), nullable=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_flow_panel_image_panel_id", "flow_panel_image", ["panel_id"])
    op.create_index("ix_flow_panel_image_role", "flow_panel_image", ["role"])
    op.create_index("ix_flow_panel_image_media_id", "flow_panel_image", ["media_id"])
    op.create_index("ix_flow_panel_image_created_at", "flow_panel_image", ["created_at"])

    # Notes hang off the PANEL, not a version: "Sai nơ áo" is true of the panel
    # and stays true across re-generations until someone fixes it.
    op.create_table(
        "flow_panel_note",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "panel_id",
            sa.Integer(),
            sa.ForeignKey("flow_panel.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolved_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_flow_panel_note_panel_id", "flow_panel_note", ["panel_id"])
    op.create_index("ix_flow_panel_note_resolved", "flow_panel_note", ["resolved"])
    op.create_index("ix_flow_panel_note_created_at", "flow_panel_note", ["created_at"])


def downgrade() -> None:
    op.drop_table("flow_panel_note")
    op.drop_table("flow_panel_image")
    op.drop_table("flow_panel")
    op.drop_table("flow_project_member")
    op.drop_table("flow_project")
