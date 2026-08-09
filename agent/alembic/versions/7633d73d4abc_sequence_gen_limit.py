"""a sequence may be unlocked past the house generation limit

NULL means the default. A number is an unlock a PM granted after looking at why
the first attempts did not work — stored per sequence, because that is the thing
being unlocked.

Revision ID: 7633d73d4abc
Revises: 258f696432ec
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7633d73d4abc"
down_revision: Union[str, Sequence[str], None] = "258f696432ec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("shot", sa.Column("gen_limit", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("shot", "gen_limit")
