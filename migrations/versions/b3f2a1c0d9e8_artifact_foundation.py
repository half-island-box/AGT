"""artifact_foundation

Revision ID: b3f2a1c0d9e8
Revises: e0790e0844e7
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f2a1c0d9e8'
down_revision: Union[str, Sequence[str], None] = 'e0790e0844e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: Phase 2 artifact foundation.

    - Create ``artifacts`` table (declared Step deliverables registry).
    - Add ``tasks.input_message`` (normalized A2A-shaped Message JSON).
    - Purely additive: no backfill, no behavior change for existing rows
      (``input_message`` stays NULL on legacy Steps).
    """
    op.create_table(
        'artifacts',
        sa.Column('artifact_id', sa.String(length=64), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('step_id', sa.String(length=64), nullable=False),
        sa.Column('producer_identity_id', sa.String(length=256), nullable=True),
        sa.Column('name', sa.String(length=512), nullable=False),
        sa.Column('mime_type', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('status_flag', sa.String(length=32), nullable=True),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('storage_path', sa.String(length=1024), nullable=True),
        sa.Column('inline_bytes', sa.LargeBinary(), nullable=True),
        sa.Column('local_path', sa.String(length=1024), nullable=True),
        sa.Column('origin_daemon_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='registered'),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.job_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['step_id'], ['tasks.task_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('artifact_id'),
    )
    op.create_index(op.f('ix_artifacts_job'), 'artifacts', ['job_id'], unique=False)
    op.create_index(op.f('ix_artifacts_step'), 'artifacts', ['step_id'], unique=False)
    op.create_index(op.f('ix_artifacts_status'), 'artifacts', ['status'], unique=False)

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('input_message', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('input_message')
    op.drop_index(op.f('ix_artifacts_status'), table_name='artifacts')
    op.drop_index(op.f('ix_artifacts_step'), table_name='artifacts')
    op.drop_index(op.f('ix_artifacts_job'), table_name='artifacts')
    op.drop_table('artifacts')
