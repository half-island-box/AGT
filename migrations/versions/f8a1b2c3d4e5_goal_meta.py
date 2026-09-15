"""goal_meta

Revision ID: f8a1b2c3d4e5
Revises: d6e9f3a2b7c8
Create Date: 2026-09-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8a1b2c3d4e5'
down_revision: Union[str, Sequence[str], None] = 'd6e9f3a2b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: goal mode root metadata.

    ``jobs.goal_meta`` JSON — per-run spawn budget (max_depth /
    max_descendants / max_batch), template fingerprint and the raw user
    goal text (goal export reads it without un-injecting the
    orchestrator template). Purely additive; NULL on non-goal jobs.
    """
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('goal_meta', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('goal_meta')
