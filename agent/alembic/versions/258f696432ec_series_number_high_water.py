"""remember the highest comic number a slate has issued

Comic names carry a five-digit number that ends up in exported folder names.
Deriving the next one from the highest currently IN USE hands a deleted comic's
number straight to the next one, and then two different comics share an
identifier people say out loud.

Seeded from the numbers already present, so existing slates carry on from where
they are rather than starting again at 001.

Revision ID: 258f696432ec
Revises: 08b2e8a56cd0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "258f696432ec"
down_revision: Union[str, Sequence[str], None] = "08b2e8a56cd0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("flow_project", sa.Column("last_series_seq", sa.Integer(), nullable=True))
    op.get_bind().execute(sa.text("""
        UPDATE flow_project p
        SET last_series_seq = sub.hi
        FROM (
            SELECT project_id,
                   MAX(CAST(SUBSTRING(name FROM '^GCSA_([0-9]{5})_') AS INTEGER)) AS hi
            FROM flow_series
            WHERE name ~ '^GCSA_[0-9]{5}_'
            GROUP BY project_id
        ) sub
        WHERE sub.project_id = p.id
    """))


def downgrade() -> None:
    op.drop_column("flow_project", "last_series_seq")
