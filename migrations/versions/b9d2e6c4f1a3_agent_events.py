"""agent events and token usage

Revision ID: b9d2e6c4f1a3
Revises: a7c1e5b90d42
Create Date: 2026-10-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9d2e6c4f1a3'
down_revision: Union[str, Sequence[str], None] = 'a7c1e5b90d42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: structured agent events + per-turn usage.

    ``step_events`` stores thinking / tool / usage events (never text —
    text lives in ``tasks.stdout``). The usage columns on ``tasks`` hold the
    provider's terminal turn aggregate, which is authoritative and is never
    summed with the per-call ``message`` usage events.
    """
    for name, column_type in (
        ('input_tokens', sa.Integer()),
        ('output_tokens', sa.Integer()),
        ('cache_read_tokens', sa.Integer()),
        ('cache_creation_tokens', sa.Integer()),
        ('cost_usd', sa.Float()),
        ('model', sa.String(length=128)),
        ('num_turns', sa.Integer()),
    ):
        op.add_column('tasks', sa.Column(name, column_type, nullable=True))

    op.create_table(
        'step_events',
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('text_offset', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('payload_json', sa.Text(), nullable=False,
                  server_default='{}'),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint('task_id', 'seq'),
        sa.ForeignKeyConstraint(
            ['task_id'], ['tasks.task_id'], ondelete='CASCADE'
        ),
    )


def downgrade() -> None:
    op.drop_table('step_events')
    for name in (
        'num_turns',
        'model',
        'cost_usd',
        'cache_creation_tokens',
        'cache_read_tokens',
        'output_tokens',
        'input_tokens',
    ):
        op.drop_column('tasks', name)
