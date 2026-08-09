"""lead and PM are one role

The studio treats a lead and a PM as the same person, so the two were one job
under two names with a line between them nobody could state — a lead could
rename a series but not create one, and delete a sequence but not the episode
holding it.

Stored rows are rewritten to `producer`. The read path already maps `lead` UP to
producer (see `normalize_role`), so this is not what makes the app correct — it
is what stops the database describing a role that no longer exists. Mapping up
and not down matters: the unknown-value fallback is `artist`, so a lead left
unhandled would have been silently demoted.

Revision ID: aadf1ba7a6c5
Revises: 2d2c0aaf5010
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "aadf1ba7a6c5"
down_revision: Union[str, Sequence[str], None] = "2d2c0aaf5010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    for table in ("project_member", "flow_series_member"):
        n = conn.execute(
            sa.text(f"UPDATE {table} SET role = 'producer' WHERE role = 'lead'")
        ).rowcount
        if n:
            print(f"  {table}: {n} lead row(s) → producer")


def downgrade() -> None:
    # Not reversible: which producers were leads is not recorded anywhere, and
    # guessing would hand somebody the wrong rights.
    pass
