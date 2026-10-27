"""subjob_lineage

Revision ID: c4d8e2f1a0b9
Revises: b3f2a1c0d9e8
Create Date: 2026-09-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d8e2f1a0b9'
down_revision: Union[str, Sequence[str], None] = 'b3f2a1c0d9e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: Phase 3 Job-tree lineage + spawn payload.

    - ``jobs.parent_job_id`` (nullable FK, CASCADE, indexed).
    - ``jobs.root_job_id`` (indexed; backfilled to ``job_id`` for the
      existing depth-1 jobs — a root's root is itself).
    - ``tasks.outcome_payload`` (sidecar JSON for spawn intents).
    Purely additive; no behavior change for existing rows.
    """
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_job_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('root_job_id', sa.String(length=64), nullable=False, server_default=''))
        batch_op.create_index('ix_jobs_parent', ['parent_job_id'], unique=False)
        batch_op.create_index('ix_jobs_root', ['root_job_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_jobs_parent_job_id', 'jobs', ['parent_job_id'], ['job_id'], ondelete='CASCADE'
        )
    op.execute("UPDATE jobs SET root_job_id = job_id WHERE root_job_id = ''")

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('outcome_payload', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('outcome_payload')
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_jobs_parent_job_id', type_='foreignkey')
        batch_op.drop_index(op.f('ix_jobs_root'), table_name='jobs')
        batch_op.drop_index(op.f('ix_jobs_parent'), table_name='jobs')
        batch_op.drop_column('root_job_id')
        batch_op.drop_column('parent_job_id')
