"""console_project_remote_dir

Revision ID: d2b3c4e5f6a7
Revises: f3a7c9d1e2b4
Create Date: 2026-10-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2b3c4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'f3a7c9d1e2b4'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Optional remote working directory for a console project.

    ``path`` stays the mandatory local (Server-machine) directory. A project
    may additionally point at a directory on one remote daemon; the two
    columns are all-or-nothing, enforced in the service layer. The partial
    unique index keeps "same daemon + same directory" from appearing twice.
    """
    with op.batch_alter_table('console_projects', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('remote_daemon_id', sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column('remote_path', sa.String(length=1024), nullable=True)
        )
    op.create_index(
        'ix_console_projects_remote_daemon_id',
        'console_projects',
        ['remote_daemon_id'],
        unique=False,
    )
    op.create_index(
        'ux_console_projects_remote',
        'console_projects',
        ['remote_daemon_id', 'remote_path'],
        unique=True,
        sqlite_where=sa.text(
            'remote_daemon_id IS NOT NULL AND remote_path IS NOT NULL'
        ),
    )


def downgrade() -> None:
    op.drop_index('ux_console_projects_remote', table_name='console_projects')
    op.drop_index(
        'ix_console_projects_remote_daemon_id',
        table_name='console_projects',
    )
    with op.batch_alter_table('console_projects', schema=None) as batch_op:
        batch_op.drop_column('remote_path')
        batch_op.drop_column('remote_daemon_id')
