"""Create the app and ops schemas (ARCHITECTURE.md §5.2).

`ops` already exists when this runs: env.py creates it, because it holds this chain's version
table. Their tables arrive with the work that uses them.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA app")
    op.execute("CREATE SCHEMA IF NOT EXISTS ops")


def downgrade() -> None:
    op.execute("DROP SCHEMA app")
