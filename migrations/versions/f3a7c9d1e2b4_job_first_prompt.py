"""job first prompt

Revision ID: f3a7c9d1e2b4
Revises: c9e0f1a2b3d4
Create Date: 2026-10-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3a7c9d1e2b4"
down_revision: Union[str, Sequence[str], None] = "c9e0f1a2b3d4"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add and backfill the immutable first prompt for chat conversations."""
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("first_prompt", sa.Text(), nullable=True))

    op.get_bind().execute(sa.text("""
        UPDATE jobs
        SET first_prompt = (
            SELECT t.prompt
            FROM tasks AS t
            WHERE t.job_id = jobs.job_id
            ORDER BY t.created_at ASC, t.task_id ASC
            LIMIT 1
        )
        WHERE job_type = 'chat'
          AND (first_prompt IS NULL OR first_prompt = '')
    """))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("first_prompt")
