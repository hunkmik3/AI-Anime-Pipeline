"""Persistent project assistant conversations and action receipts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'c83a7d9e0412'
down_revision = 'b728a91d6c03'
branch_labels = depends_on = None

def upgrade():
    if sa.inspect(op.get_bind()).has_table('automation_assistant_turn'):
        return
    j = sa.JSON().with_variant(JSONB(), 'postgresql')
    op.create_table('automation_assistant_turn',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('project_id', sa.Uuid(), sa.ForeignKey('automation_project.id', ondelete='CASCADE'), nullable=False),
        sa.Column('request_key', sa.String(), nullable=False),
        sa.Column('message', sa.String(), nullable=False),
        sa.Column('reply', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('context', j, nullable=False),
        sa.Column('events', j, nullable=False),
        sa.Column('error', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('project_id', 'request_key', name='uq_assistant_turn_request'))
    op.create_index('ix_automation_assistant_turn_project_id', 'automation_assistant_turn', ['project_id'])
    op.create_index('ix_automation_assistant_turn_status', 'automation_assistant_turn', ['status'])

def downgrade():
    op.drop_table('automation_assistant_turn')
