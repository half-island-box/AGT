"""sop_loop_columns

Revision ID: da3815c5bc54
Revises: f8a1b2c3d4e5
Create Date: 2026-10-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'da3815c5bc54'
down_revision: Union[str, Sequence[str], None] = 'f8a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add SOP loop tracking columns to jobs.

    ``sop_loop_name`` identifies which declared loop a job belongs to.
    ``sop_iteration`` is 1..N for loop iterations, 0 for ordinary DAG nodes.
    ``sop_loop_meta`` is a duplicated JSON blob with loop-level config so
    the loop controller can recover state without a separate table.
    """
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sop_loop_name', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('sop_iteration', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('sop_loop_meta', sa.Text(), nullable=True))
        batch_op.create_index('ix_jobs_sop_loop_name', ['sop_loop_name'], unique=False)
        batch_op.create_index('ix_jobs_sop_iteration', ['sop_iteration'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index('ix_jobs_sop_iteration')
        batch_op.drop_index('ix_jobs_sop_loop_name')
        batch_op.drop_column('sop_loop_meta')
        batch_op.drop_column('sop_iteration')
        batch_op.drop_column('sop_loop_name')
