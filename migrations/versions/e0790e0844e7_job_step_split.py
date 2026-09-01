"""job_step_split

Revision ID: e0790e0844e7
Revises: 0d4dd910224d
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e0790e0844e7'
down_revision: Union[str, Sequence[str], None] = '0d4dd910224d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: introduce Job/Step split.

    - Create ``jobs`` table.
    - Add ``job_id`` and ``outcome`` columns to ``tasks``.
    - Backfill a Job for every legacy Task (one-to-one).
    - Rebuild ``sessions`` with a ``job_id``-based composite PK.
    """
    # 1. Create jobs table.
    op.create_table(
        'jobs',
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('job_type', sa.String(length=16), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('identity_did', sa.String(length=256), nullable=True),
        sa.Column('target_daemon_id', sa.String(length=64), nullable=True),
        sa.Column('work_dir', sa.String(length=512), nullable=True),
        sa.Column('agent_cwd', sa.String(length=1024), nullable=True),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.Column('updated_at', sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint('job_id')
    )
    op.create_index(op.f('ix_jobs_identity'), 'jobs', ['identity_did'], unique=False)
    op.create_index(op.f('ix_jobs_provider'), 'jobs', ['provider'], unique=False)
    op.create_index(op.f('ix_jobs_status'), 'jobs', ['status'], unique=False)

    # 2. Add job_id / outcome columns to tasks.
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('job_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('outcome', sa.Text(), nullable=True))
        batch_op.create_index(op.f('ix_tasks_job'), ['job_id'], unique=False)

    # 3. Backfill jobs for legacy tasks and link them.
    op.execute("""
        INSERT OR IGNORE INTO jobs (job_id, status, job_type, provider, identity_did, target_daemon_id, work_dir, agent_cwd, created_at, updated_at)
        SELECT task_id, status, 'task', provider, identity_did, target_daemon_id, work_dir, agent_cwd, created_at, updated_at
        FROM tasks
        WHERE job_id IS NULL
    """)
    op.execute("""
        UPDATE tasks SET job_id = task_id WHERE job_id IS NULL
    """)

    # 4. Rebuild sessions table with job_id PK. SQLite requires recreating
    #    the table to change the primary key.
    op.execute("""
        CREATE TABLE sessions_new (
            job_id TEXT NOT NULL,
            identity_did TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (job_id, identity_did, provider),
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        INSERT INTO sessions_new
            (job_id, identity_did, provider, session_id, created_at, updated_at)
        SELECT
            t.job_id,
            COALESCE(s.identity_did, '') AS identity_did,
            s.provider,
            s.session_id,
            s.created_at,
            COALESCE(s.updated_at, s.created_at) AS updated_at
        FROM sessions s
        JOIN tasks t ON t.task_id = s.task_id
    """)
    op.execute("DROP TABLE sessions")
    op.execute("ALTER TABLE sessions_new RENAME TO sessions")
    op.create_index(op.f('ix_sessions_job'), 'sessions', ['job_id'], unique=False)
    op.create_index(op.f('ix_sessions_session'), 'sessions', ['session_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema: undo Job/Step split.

    Note: this is lossy — multiple Steps per Job are collapsed back into a
    single Task row. It preserves the Job's top-level metadata and drops the
    additional Step rows.
    """
    # Rebuild sessions with task_id PK.
    op.execute("""
        CREATE TABLE sessions_new (
            task_id TEXT NOT NULL,
            identity_did TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (task_id, identity_did, provider)
        )
    """)
    op.execute("""
        INSERT INTO sessions_new
            (task_id, identity_did, provider, session_id, created_at, updated_at)
        SELECT
            t.task_id,
            s.identity_did,
            s.provider,
            s.session_id,
            s.created_at,
            s.updated_at
        FROM sessions s
        JOIN tasks t ON t.job_id = s.job_id
    """)
    op.execute("DROP TABLE sessions")
    op.execute("ALTER TABLE sessions_new RENAME TO sessions")

    # Drop job_id/outcome columns from tasks.
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_index(op.f('ix_tasks_job'))
        batch_op.drop_column('outcome')
        batch_op.drop_column('job_id')

    # Drop jobs table.
    op.drop_index(op.f('ix_jobs_status'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_provider'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_identity'), table_name='jobs')
    op.drop_table('jobs')
