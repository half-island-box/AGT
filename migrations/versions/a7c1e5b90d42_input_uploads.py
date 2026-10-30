"""input_uploads

Revision ID: a7c1e5b90d42
Revises: 44d26c234725
Create Date: 2026-10-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c1e5b90d42'
down_revision: Union[str, Sequence[str], None] = '44d26c234725'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: user-uploaded chat attachments.

    Server-owned input files, deliberately separate from ``artifacts``
    (which are Step *outputs* with mandatory job/step lineage). Bytes are
    inline (<4KB) or on the Server disk; ``sha256`` is unique so identical
    content is stored once.
    """
    op.create_table(
        'input_uploads',
        sa.Column('upload_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=512), nullable=False),
        sa.Column('mime_type', sa.String(length=128), nullable=False,
                  server_default=''),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('storage_path', sa.String(length=1024), nullable=True),
        sa.Column('inline_bytes', sa.LargeBinary(), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=False,
                  server_default=''),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.Column('expires_at', sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint('upload_id'),
    )
    op.create_index(
        'ix_input_uploads_sha256', 'input_uploads', ['sha256'], unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_input_uploads_sha256', table_name='input_uploads')
    op.drop_table('input_uploads')
