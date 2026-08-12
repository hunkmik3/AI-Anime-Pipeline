"""submission.kind — an artist's hand-over and an editor's are different things

The editor takes the generated clips, assembles the episode outside the app and
hands one file back. That is a second hand-over on the same series, and it is not
another attempt at the first: an artist's v2 and an editor's v2 are different
rounds of different work, so they cannot share a version sequence or a queue.

Existing rows are ``cut`` — every submission written before this was an artist
handing generated work over, which is what that word means.

Revision ID: e5b2c8a71f34
Revises: c4e1a9f6b2d8
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5b2c8a71f34"
down_revision = "c4e1a9f6b2d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("submission")}
    if "kind" in cols:
        return
    with op.batch_alter_table("submission") as b:
        b.add_column(
            sa.Column("kind", sa.String(), nullable=False, server_default="cut")
        )
    op.create_index("ix_submission_kind", "submission", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_submission_kind", table_name="submission")
    with op.batch_alter_table("submission") as b:
        b.drop_column("kind")
