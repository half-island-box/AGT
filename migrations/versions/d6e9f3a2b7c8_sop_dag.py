"""sop_dag

Revision ID: d6e9f3a2b7c8
Revises: c4d8e2f1a0b9
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd6e9f3a2b7c8'
down_revision: Union[str, Sequence[str], None] = 'c4d8e2f1a0b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: Phase 5 SOP static DAG.

    - ``job_parents`` association table (DAG dependency edges; row
      (job_id=X, parent_job_id=Y) = X waits for Y).
    - ``jobs.sop_node`` per-node activation metadata JSON (raw input
      placeholders, output name, explicit routing overrides) — needed
      because blocked jobs have no Step until activation.
    Purely additive.
    """
    op.create_table(
        'job_parents',
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('parent_job_id', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.job_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parent_job_id'], ['jobs.job_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('job_id', 'parent_job_id'),
    )
    op.create_index(
        op.f('ix_job_parents_job'), 'job_parents', ['job_id'], unique=False
    )
    op.create_index(
        op.f('ix_job_parents_parent'), 'job_parents', ['parent_job_id'], unique=False
    )
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sop_node', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('sop_node')
    op.drop_index(op.f('ix_job_parents_parent'), table_name='job_parents')
    op.drop_index(op.f('ix_job_parents_job'), table_name='job_parents')
    op.drop_table('job_parents')
