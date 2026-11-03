"""allow drafts for brand-new emails (no thread yet)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("email_drafts", "thread_id", nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM email_drafts WHERE thread_id IS NULL")
    op.alter_column("email_drafts", "thread_id", nullable=False)
