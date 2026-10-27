"""goal_shared_context

Revision ID: a4f7c8d9e1b2
Revises: 44d26c234725
Create Date: 2026-10-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4f7c8d9e1b2'
down_revision: Union[str, Sequence[str], None] = '44d26c234725'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Upgrade schema: Goal shared context entries and Step Brief audit."""
    op.create_table(
        'context_entries',
        sa.Column('entry_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('root_job_id', sa.String(length=64), nullable=False),
        sa.Column('author_job_id', sa.String(length=64), nullable=False),
        sa.Column('author_step_id', sa.String(length=64), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('key', sa.String(length=256), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('content_json', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['root_job_id'], ['jobs.job_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('entry_id'),
        sa.UniqueConstraint(
            'author_step_id',
            'idempotency_key',
            name='ux_context_entries_step_idempotency',
        ),
    )
    op.create_index(
        'ix_context_entries_root_id', 'context_entries',
        ['root_job_id', 'entry_id'], unique=False,
    )
    op.create_index(
        'ix_context_entries_root_key', 'context_entries',
        ['root_job_id', 'key', 'status'], unique=False,
    )
    op.create_index(
        'ix_context_entries_root_author', 'context_entries',
        ['root_job_id', 'author_job_id'], unique=False,
    )
    op.create_index(
        'ix_context_entries_key', 'context_entries', ['key'], unique=False
    )
    op.create_index(
        'ix_context_entries_status', 'context_entries', ['status'], unique=False
    )
    op.create_index(
        'ix_context_entries_author_job_id', 'context_entries',
        ['author_job_id'], unique=False,
    )
    op.create_index(
        'ix_context_entries_root_job_id', 'context_entries',
        ['root_job_id'], unique=False,
    )
    op.create_index(
        'ux_context_entries_active_key', 'context_entries',
        ['root_job_id', 'key'], unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('context_brief_json', sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column('context_snapshot_id', sa.String(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column('context_watermark', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('context_watermark')
        batch_op.drop_column('context_snapshot_id')
        batch_op.drop_column('context_brief_json')

    op.drop_index(
        'ux_context_entries_active_key', table_name='context_entries'
    )
    op.drop_index(
        'ix_context_entries_root_job_id', table_name='context_entries'
    )
    op.drop_index(
        'ix_context_entries_author_job_id', table_name='context_entries'
    )
    op.drop_index('ix_context_entries_status', table_name='context_entries')
    op.drop_index('ix_context_entries_key', table_name='context_entries')
    op.drop_index(
        'ix_context_entries_root_author', table_name='context_entries'
    )
    op.drop_index(
        'ix_context_entries_root_key', table_name='context_entries'
    )
    op.drop_index(
        'ix_context_entries_root_id', table_name='context_entries'
    )
    op.drop_table('context_entries')
