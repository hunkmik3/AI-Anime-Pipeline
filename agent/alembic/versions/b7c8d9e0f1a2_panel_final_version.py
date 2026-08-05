"""giantflow: which version a panel is actually delivering

Until now a panel recorded that it had been submitted but not WHAT was
submitted, and every surface fell back to ``latest_generated()`` — the most
recent image. An artist who generated ten tries and preferred the seventh had no
way to say so: they submitted, and the PM was shown the tenth.

``final_media_id`` is that missing fact. Submitting means picking, so the column
is set by the submit action rather than by a separate "mark as final" step, and
approval/export read it instead of guessing.

Nullable, and left NULL for existing rows on purpose: for a panel submitted
before this migration nobody ever made a choice, so recording one would be
inventing it. Readers fall back to the latest version exactly as they did
before, which is what those panels were submitted under.

Revision ID: b7c8d9e0f1a2
Revises: f4a5b6c7d8e9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("flow_panel", sa.Column("final_media_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("flow_panel", "final_media_id")
