"""a studio board belongs to someone

The Flow Studio came over from manga_extract as one shared board list with no
owner column — so there was nothing to authorise against, and any signed-in
account could open, rename or delete anyone's board. That was a stated decision
at the time, not an oversight, and it held while the studio was one team sharing
one key. It stops holding the moment two branches with different people use it.

Nullable, and NULL means nobody's: a board created before this column existed
has no owner to name, and picking one would be a guess written into the
database. Those stay visible to admins only.

Revision ID: 05289da5c4a9
Revises: 750b89b7603b
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "05289da5c4a9"
down_revision: Union[str, Sequence[str], None] = "750b89b7603b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "flow_board",
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_flow_board_owner", "flow_board", "app_user", ["owner_user_id"], ["id"]
    )
    op.create_index(
        "ix_flow_board_owner_user_id", "flow_board", ["owner_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_flow_board_owner_user_id", table_name="flow_board")
    op.drop_constraint("fk_flow_board_owner", "flow_board", type_="foreignkey")
    op.drop_column("flow_board", "owner_user_id")
