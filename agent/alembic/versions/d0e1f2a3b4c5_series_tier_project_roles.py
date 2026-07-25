"""series tier + per-project roles

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-24

Phase 10 — two changes that go together because they define who builds what:

1. **Series tier.** ``Project → Series → Episode|Chapter → Sequence``. The new
   ``series`` table sits between project and scene; ``scene.series_id`` points
   at it, and ``scene.code`` / ``shot.code`` carry the human codes ("EP007",
   "SQ03"). Every existing project gets one ``Default`` series and all of its
   scenes are attached to it, so nothing is orphaned and the UI never has to
   render a series-less episode.

2. **Per-project roles.** ``project_member.role`` — producer | lead | artist |
   viewer. Existing members become ``lead``: before this migration a member had
   no structural rights at all (structure was admin-only), so ``lead`` is the
   role that lets them keep working *and* start building the structure
   themselves, which is the point of the change. The project's
   ``owner_user_id`` is an implicit producer and gets no row.

Additive only — every new column is nullable or defaulted, so the running
server keeps serving while this applies.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. series table ───────────────────────────────────────────────────
    op.create_table(
        "series",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("code", sa.String(), nullable=False, server_default=""),
        sa.Column("unit_label", sa.String(), nullable=False, server_default="Episode"),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_series_project_id", "series", ["project_id"])

    # ── 2. scene / shot columns ───────────────────────────────────────────
    op.add_column(
        "scene", sa.Column("series_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_scene_series_id", "scene", "series", ["series_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_scene_series_id", "scene", ["series_id"])
    op.add_column(
        "scene", sa.Column("code", sa.String(), nullable=False, server_default="")
    )
    op.add_column(
        "shot", sa.Column("code", sa.String(), nullable=False, server_default="")
    )

    # ── 3. per-project role ───────────────────────────────────────────────
    op.add_column(
        "project_member",
        sa.Column("role", sa.String(), nullable=False, server_default="artist"),
    )
    # Pre-existing members predate roles entirely — give them `lead` so the
    # change only ever widens what they can do (see module docstring).
    op.execute("UPDATE project_member SET role = 'lead'")

    # ── 4. backfill: one Default series per project, adopt every scene ────
    # gen_random_uuid() is core since PG13; the deployment is PG16.
    op.execute(
        """
        INSERT INTO series (id, project_id, name, code, unit_label, order_index,
                            settings, created_at)
        SELECT gen_random_uuid(), p.id, 'Default', '', 'Episode', 0, '{}'::jsonb, now()
        FROM project p
        """
    )
    op.execute(
        """
        UPDATE scene sc
        SET series_id = se.id
        FROM series se
        WHERE se.project_id = sc.project_id AND sc.series_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("project_member", "role")
    op.drop_column("shot", "code")
    op.drop_column("scene", "code")
    op.drop_index("ix_scene_series_id", table_name="scene")
    op.drop_constraint("fk_scene_series_id", "scene", type_="foreignkey")
    op.drop_column("scene", "series_id")
    op.drop_index("ix_series_project_id", table_name="series")
    op.drop_table("series")
