"""shot: production bag, so a Sequence can carry its own credit ceiling

Diagram 5 asks "does this **Sequence** have quota left?" and diagram 1 has the PM
set a quota when creating an Episode. Budgets existed only at Project and Series,
so neither question could be answered.

Series and Scene already keep this data in a ``production`` JSONB bag; giving
Shot the same shape means all four tiers are uniform and the budget code stays
one code path instead of four special cases.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "shot",
        sa.Column(
            "production",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("shot", "production")
