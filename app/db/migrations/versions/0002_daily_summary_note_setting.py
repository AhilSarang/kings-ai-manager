"""daily summary note setting

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO settings (key, value) VALUES ('daily_summary_llm_note', 'true'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key = 'daily_summary_llm_note'")
