"""edit_note — the editor's notes on a frame of their cut

SyncSketch in one table. `shot_id` is the point of the feature: which sequence the
note is really about. It is CHOSEN by the editor, not computed from the timecode —
the cut is assembled outside the app, so trims and reorders make arithmetic
silently wrong, and the editor is already paused on the frame.

Revision ID: f7d3a91c2e58
Revises: e5b2c8a71f34
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7d3a91c2e58"
down_revision = "e5b2c8a71f34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "edit_note" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "edit_note",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("shot_id", sa.Uuid(), nullable=True),
        sa.Column("at_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("body", sa.String(), nullable=False, server_default=""),
        sa.Column("drawing_media_id", sa.String(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("author_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    for col in ("submission_id", "shot_id", "resolved", "author_user_id", "created_at"):
        op.create_index(f"ix_edit_note_{col}", "edit_note", [col])
    if bind.dialect.name != "sqlite":
        # The cut going away takes its notes; the SEQUENCE going away only
        # unhooks them, because the note still records that something was wrong
        # and deleting that history with a sequence would hide a round of work.
        op.create_foreign_key("fk_edit_note_submission", "edit_note", "submission",
                              ["submission_id"], ["id"], ondelete="CASCADE")
        op.create_foreign_key("fk_edit_note_shot", "edit_note", "shot",
                              ["shot_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_table("edit_note")
