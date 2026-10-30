"""console_projects

Revision ID: b8c4d5e6f7a9
Revises: a4f7c8d9e1b2
Create Date: 2026-10-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8c4d5e6f7a9'
down_revision: Union[str, Sequence[str], None] = 'a4f7c8d9e1b2'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Store browser-console project directory pointers."""
    op.create_table(
        'console_projects',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('path', sa.String(length=1024), nullable=False),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.Column('updated_at', sa.String(length=32), nullable=False),
        sa.Column('last_opened_at', sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('path', name='ux_console_projects_path'),
    )
    op.create_index(
        'ix_console_projects_last_opened_at',
        'console_projects',
        ['last_opened_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        'ix_console_projects_last_opened_at',
        table_name='console_projects',
    )
    op.drop_table('console_projects')
