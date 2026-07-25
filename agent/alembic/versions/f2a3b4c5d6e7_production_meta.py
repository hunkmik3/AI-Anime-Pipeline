"""production-tracking metadata on series + scene

Revision ID: f2a3b4c5d6e7
Revises: d0e1f2a3b4c5
Create Date: 2026-07-25

Phase 10 CRM: adds a ``production`` JSONB bag to ``series`` and ``scene`` holding
the fields the studio kept in the Series_Master / Episode_Tracker spreadsheets
(series: tier, status, priority, dates, folder link, genres, tropes, markets,
audience, language, logline, planned episodes, episode duration; episode:
pipeline status + the four role assignees + duration/links/deadline/complete).

A JSONB bag rather than ~25 typed columns so the field set can track the sheet
without a migration each time; known keys are validated in the services.
(SQLite/bundled builds get the column via SQLModel.metadata.create_all.)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add(table: str) -> None:
    op.add_column(
        table,
        sa.Column(
            "production",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def upgrade() -> None:
    _add("series")
    _add("scene")


def downgrade() -> None:
    op.drop_column("scene", "production")
    op.drop_column("series", "production")
