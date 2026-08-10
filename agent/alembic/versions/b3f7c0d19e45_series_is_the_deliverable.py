"""the series is the deliverable, not the episode

One person takes a series and hands in the finished thing once; a PM reviews it
once rather than signing off twelve times. So `submission` points at a series and
`series` carries the lifecycle state the episode used to.

`submission.scene_id` becomes nullable rather than being dropped: rows written
under the old rule described an episode, and rewriting history to point somewhere
it never pointed is worse than a column that is empty on new rows. Existing rows
are given the series their episode belongs to, so nothing loses its place in a
list.

`scene.deliverable_status` is left alone and stops being read. Dropping it would
take the old rows' state with it for no gain, and the reader now asks the series.

Revision ID: b3f7c0d19e45
Revises: 9c1d4e77af02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b3f7c0d19e45"
down_revision = "9c1d4e77af02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    series_cols = {c["name"] for c in insp.get_columns("series")}
    if "deliverable_status" not in series_cols:
        with op.batch_alter_table("series") as b:
            b.add_column(
                sa.Column(
                    "deliverable_status",
                    sa.String(),
                    nullable=False,
                    server_default="draft",
                )
            )
        op.create_index(
            "ix_series_deliverable_status", "series", ["deliverable_status"]
        )

    sub_cols = {c["name"] for c in insp.get_columns("submission")}
    if "series_id" not in sub_cols:
        with op.batch_alter_table("submission") as b:
            b.add_column(sa.Column("series_id", sa.Uuid(), nullable=True))
        op.create_index("ix_submission_series_id", "submission", ["series_id"])
        if bind.dialect.name != "sqlite":
            op.create_foreign_key(
                "fk_submission_series",
                "submission",
                "series",
                ["series_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # Old rows keep their place: the series is the one their episode sits in.
    op.execute(
        sa.text(
            "UPDATE submission SET series_id = ("
            "  SELECT scene.series_id FROM scene WHERE scene.id = submission.scene_id"
            ") WHERE series_id IS NULL AND scene_id IS NOT NULL"
        )
    )

    # New rows carry no episode, so the column cannot stay NOT NULL.
    if bind.dialect.name != "sqlite":
        op.alter_column("submission", "scene_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.alter_column(
            "submission", "scene_id", existing_type=sa.Uuid(), nullable=False
        )
        op.drop_constraint("fk_submission_series", "submission", type_="foreignkey")
    op.drop_index("ix_submission_series_id", table_name="submission")
    with op.batch_alter_table("submission") as b:
        b.drop_column("series_id")
    op.drop_index("ix_series_deliverable_status", table_name="series")
    with op.batch_alter_table("series") as b:
        b.drop_column("deliverable_status")
