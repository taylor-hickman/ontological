"""ops.llm_call: agent sessions are logged too (ARCHITECTURE.md §6.1, §8.1). An `agent` row is one
answer-agent session, logged like any spawn and never served from the cache.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ops.llm_call DROP CONSTRAINT llm_call_kind_check")
    op.execute(
        "ALTER TABLE ops.llm_call ADD CONSTRAINT llm_call_kind_check "
        "CHECK (kind IN ('structured', 'text', 'agent'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM ops.llm_call WHERE kind = 'agent'")
    op.execute("ALTER TABLE ops.llm_call DROP CONSTRAINT llm_call_kind_check")
    op.execute(
        "ALTER TABLE ops.llm_call ADD CONSTRAINT llm_call_kind_check "
        "CHECK (kind IN ('structured', 'text'))"
    )
