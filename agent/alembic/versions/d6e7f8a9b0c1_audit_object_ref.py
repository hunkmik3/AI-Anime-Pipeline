"""audit_log: reference the object a change was made to

The security trail was user-centric (actor → target user), which answers "who
logged in" but not "who reassigned Ep03, and when". Production work needs the
second question: the studio's whole reason for leaving Google Sheets was that
edits overwrote each other with no trail.

Adding object_type/object_id to the existing table rather than starting a second
log keeps one timeline per thing — a project's history then includes both its
role changes (security) and its budget changes (production), which is how people
actually ask about it.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable: every existing security row keeps working with these unset.
    op.add_column("audit_log", sa.Column("object_type", sa.String(), nullable=True))
    op.add_column("audit_log", sa.Column("object_id", sa.String(), nullable=True))
    # The lookup this exists for: "show me everything that happened to X".
    op.create_index(
        "ix_audit_log_object", "audit_log", ["object_type", "object_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_object", table_name="audit_log")
    op.drop_column("audit_log", "object_id")
    op.drop_column("audit_log", "object_type")
