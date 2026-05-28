"""Phase 8.3: drop scene.scene_bible_text, add scene.canvas_state

Scene Bible is removed (user-confirmed not needed; Manual mode doesn't run
Phase 6 bible injection). canvas_state holds the multi-shot SceneCanvas
layout — shot_groups[] = [{shot_id, position, collapsed, label, order}].
master_establishing_asset_id is unrelated and kept.

Revision ID: f1a2b3c4d5e6
Revises: 86e7b45a4366
Create Date: 2026-05-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "86e7b45a4366"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scene",
        sa.Column(
            "canvas_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.drop_column("scene", "scene_bible_text")


def downgrade() -> None:
    op.add_column(
        "scene",
        sa.Column("scene_bible_text", sa.Text(), nullable=False, server_default=""),
    )
    op.drop_column("scene", "canvas_state")
