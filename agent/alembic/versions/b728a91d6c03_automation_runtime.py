"""Durable automation jobs, optimistic board revisions and production manifests."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = 'b728a91d6c03'
down_revision = 'f0a1b2c3d4e5'
branch_labels = depends_on = None
J = sa.JSON().with_variant(JSONB(), 'postgresql')
def upgrade():
    bind = op.get_bind()
    if 'revision' not in {c['name'] for c in sa.inspect(bind).get_columns('automation_project')}:
        op.add_column('automation_project', sa.Column('revision', sa.Integer(), nullable=False, server_default='0'))
    if not sa.inspect(bind).has_table('automation_job'):
        op.create_table('automation_job',
            sa.Column('id', sa.Uuid(), primary_key=True),
            sa.Column('project_id', sa.Uuid(), sa.ForeignKey('automation_project.id'), nullable=True),
            sa.Column('request_key', sa.String(), nullable=False), sa.Column('kind', sa.String(), nullable=False),
            sa.Column('node_id', sa.String(), nullable=False), sa.Column('slot', sa.String(), nullable=False),
            sa.Column('payload', J, nullable=False), sa.Column('status', sa.String(), nullable=False),
            sa.Column('provider_job_id', sa.String(), nullable=False), sa.Column('prepared', J, nullable=False),
            sa.Column('result', J, nullable=False), sa.Column('error', sa.String(), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('lease_token', sa.String(), nullable=False),
            sa.Column('lease_until', sa.DateTime(timezone=True)), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint('project_id', 'request_key', name='uq_automation_job_request'))
        for c in ('project_id','status','lease_until'):op.create_index('ix_automation_job_'+c,'automation_job',[c])
    if not sa.inspect(bind).has_table('automation_revision'):
        op.create_table('automation_revision', sa.Column('id',sa.Uuid(),primary_key=True),
            sa.Column('project_id',sa.Uuid(),sa.ForeignKey('automation_project.id'),nullable=False),
            sa.Column('revision',sa.Integer(),nullable=False), sa.Column('manifest',J,nullable=False),
            sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
            sa.UniqueConstraint('project_id','revision',name='uq_automation_revision'))
        op.create_index('ix_automation_revision_project_id','automation_revision',['project_id'])
def downgrade():
    op.drop_table('automation_revision');op.drop_table('automation_job');op.drop_column('automation_project','revision')
