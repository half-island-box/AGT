"""sop_template_association

Revision ID: c9e0f1a2b3d4
Revises: b8c4d5e6f7a9
Create Date: 2026-10-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9e0f1a2b3d4"
down_revision: Union[str, Sequence[str], None] = "b8c4d5e6f7a9"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Associate SOP runs with the template file that produced them."""
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(
            sa.Column("sop_template_path", sa.String(length=1024), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sop_template_hash", sa.String(length=128), nullable=True)
        )
    op.create_index(
        "ix_jobs_sop_template_path",
        "jobs",
        ["sop_template_path"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_sop_template_path", table_name="jobs")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("sop_template_hash")
        batch_op.drop_column("sop_template_path")
