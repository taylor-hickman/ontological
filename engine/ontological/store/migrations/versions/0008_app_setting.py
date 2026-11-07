"""app.setting: the person's settings, one JSON value per key (ARCHITECTURE.md §5.2). A key with
no row has its default, which ontological/settings.py keeps.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE TABLE app.setting (key text PRIMARY KEY, value jsonb NOT NULL)")


def downgrade() -> None:
    op.execute("DROP TABLE app.setting")
