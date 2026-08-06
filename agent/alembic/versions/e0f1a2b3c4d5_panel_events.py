"""giantflow: what happened to a panel, and when

Versions carry a timestamp and notes carry an author, but nothing recorded the
handovers themselves — who submitted, who approved, who sent it back and when.
A reviewer looking at a panel that has been round three times could see three
versions and two remarks with no way to tell which remark answered which
version, or whether the last round was ever handed back.

One row per event, append-only. Deliberately not derived from the other tables:
"the second version exists and the panel is approved" does not tell you the
second version is the approved one, and reconstructing that after the fact is
guesswork.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e0f1a2b3c4d5"
down_revision: Union[str, Sequence[str], None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "flow_panel_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "panel_id",
            sa.Integer(),
            sa.ForeignKey("flow_panel.id", ondelete="CASCADE"),
            nullable=False,
        ),
        #: submitted | approved | changes_requested | reopened | version_added
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        #: The version the event is about, when it is about one.
        sa.Column("media_id", sa.String(), nullable=True),
        #: The reason, for a send-back.
        sa.Column("body", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_flow_panel_event_panel_id", "flow_panel_event", ["panel_id"])
    op.create_index("ix_flow_panel_event_created_at", "flow_panel_event", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_flow_panel_event_created_at", table_name="flow_panel_event")
    op.drop_index("ix_flow_panel_event_panel_id", table_name="flow_panel_event")
    op.drop_table("flow_panel_event")
