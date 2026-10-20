"""unify run cwd and job_type

Revision ID: 44d26c234725
Revises: f8a1b2c3d4e5
Create Date: 2026-10-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '44d26c234725'
down_revision: Union[str, Sequence[str], None] = 'da3815c5bc54'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: unify run records by cwd and job_type.

    - ``jobs.cwd`` / ``sessions.cwd`` isolate runs by project directory.
    - ``jobs.job_type`` is widened from {"chat","task"} to
      {"chat","sop","goal","task"} so mode filtering is a single equality.
    - Existing SOP/Goal rows are backfilled from their metadata columns.
    """
    conn = op.get_bind()

    # 1. Add cwd columns.
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cwd', sa.String(length=1024), nullable=True))
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cwd', sa.String(length=1024), nullable=True))

    # 2. Backfill cwd from the best available directory column.
    conn.execute(sa.text(
        "UPDATE jobs SET cwd = COALESCE(work_dir, agent_cwd) WHERE cwd IS NULL"
    ))
    conn.execute(sa.text(
        """
        UPDATE sessions
        SET cwd = (
            SELECT j.cwd FROM jobs j WHERE j.job_id = sessions.job_id
        )
        WHERE cwd IS NULL
        """
    ))

    # 3. Backfill job_type for existing SOP/Goal records.
    conn.execute(sa.text(
        "UPDATE jobs SET job_type = 'goal' WHERE goal_meta IS NOT NULL"
    ))
    conn.execute(sa.text(
        "UPDATE jobs SET job_type = 'sop' WHERE sop_node IS NOT NULL AND goal_meta IS NULL"
    ))

    # 4. Create indexes for the unified list queries.
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.create_index('ix_jobs_cwd', ['cwd'], unique=False)
        batch_op.create_index('ix_jobs_cwd_job_type_updated_at', ['cwd', 'job_type', 'updated_at'], unique=False)
        batch_op.create_index('ix_jobs_cwd_root_job_id_updated_at', ['cwd', 'root_job_id', 'updated_at'], unique=False)
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.create_index('ix_sessions_cwd', ['cwd'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index('ix_jobs_cwd_root_job_id_updated_at')
        batch_op.drop_index('ix_jobs_cwd_job_type_updated_at')
        batch_op.drop_index('ix_jobs_cwd')
        batch_op.drop_column('cwd')
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_index('ix_sessions_cwd')
        batch_op.drop_column('cwd')
