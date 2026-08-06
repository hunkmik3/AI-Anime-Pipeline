"""giantflow: a Series holds Chapters

Five tiers, not four. A comic ships chapter by chapter, and the artists are
divided per chapter — so a batch belongs to a chapter, not to the comic as a
whole.

    Project        the studio's slate
      Series       one comic
        Chapter    one instalment of it
          Batch    one artist's share of that chapter
            Panel  the unit of review

``flow_batch.series_id`` becomes ``chapter_id``. Existing batches are not
orphaned: every series that has any gets a "Chapter 1" that adopts them, the same
shape the batch migration used when panels needed a batch.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "flow_chapter",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "series_id",
            sa.Integer(),
            sa.ForeignKey("flow_series.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("cover_media_id", sa.String(), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_flow_chapter_series_id", "flow_chapter", ["series_id"])
    op.create_index("ix_flow_chapter_order_index", "flow_chapter", ["order_index"])

    # Nullable first so the ALTER survives existing rows; filled in, then tightened.
    op.add_column("flow_batch", sa.Column("chapter_id", sa.Integer(), nullable=True))

    conn = op.get_bind()
    series = conn.execute(sa.text("SELECT DISTINCT series_id FROM flow_batch")).fetchall()
    for (series_id,) in series:
        chapter_id = conn.execute(
            sa.text(
                "INSERT INTO flow_chapter (series_id, name, order_index, created_at) "
                "VALUES (:sid, 'Chapter 1', 0, NOW()) RETURNING id"
            ),
            {"sid": series_id},
        ).scalar_one()
        conn.execute(
            sa.text("UPDATE flow_batch SET chapter_id = :cid WHERE series_id = :sid"),
            {"cid": chapter_id, "sid": series_id},
        )

    op.alter_column("flow_batch", "chapter_id", nullable=False)
    op.create_index("ix_flow_batch_chapter_id", "flow_batch", ["chapter_id"])
    op.create_foreign_key(
        "fk_flow_batch_chapter_id", "flow_batch", "flow_chapter",
        ["chapter_id"], ["id"], ondelete="CASCADE",
    )

    # The comic is now reached through the chapter; storing it on the batch too
    # would be the same fact in two places, and they drift.
    op.drop_index("ix_flow_batch_series_id", table_name="flow_batch")
    op.drop_column("flow_batch", "series_id")


def downgrade() -> None:
    op.add_column("flow_batch", sa.Column("series_id", sa.Integer(), nullable=True))
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE flow_batch SET series_id = c.series_id "
            "FROM flow_chapter c WHERE c.id = flow_batch.chapter_id"
        )
    )
    op.create_index("ix_flow_batch_series_id", "flow_batch", ["series_id"])
    op.drop_constraint("fk_flow_batch_chapter_id", "flow_batch", type_="foreignkey")
    op.drop_index("ix_flow_batch_chapter_id", table_name="flow_batch")
    op.drop_column("flow_batch", "chapter_id")
    op.drop_table("flow_chapter")
