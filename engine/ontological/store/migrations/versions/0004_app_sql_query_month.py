"""app.sql_query_month: how often each query-history statement was seen, per month (ARCHITECTURE.md
§1.3, §5.2). Upstream wrote these counts under a key no reader parsed, and its store dropped them;
see ontological/catalog/query_stats.py. One row per statement and month, `month` its first day.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.sql_query_month (
            sql_query_id text NOT NULL REFERENCES public.sql_query (id) ON DELETE CASCADE,
            month date NOT NULL CHECK (extract(day FROM month) = 1),
            count integer NOT NULL CHECK (count > 0),
            PRIMARY KEY (sql_query_id, month)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE app.sql_query_month")
