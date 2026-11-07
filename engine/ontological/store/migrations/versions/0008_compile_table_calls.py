"""`table_calls` on ops.job and ops.job_step: how a compile asks about its tables, split or
merged (ARCHITECTURE.md §6.4, §6.5; BUILD_PLAN.md M3.7).

A compile job's `table_calls` is the option its remaining tables take, which the person can
switch from the estimate while it runs. A step's is the option its table ran with, set once the
pack starts, so the Jobs screen counts the right stages for it: a run can mix the two, since a
resumed or switched job takes the other for the packs left, and merged leaves some tables to
split. NULL until the step's pack starts.

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
    op.execute(
        "ALTER TABLE ops.job ADD COLUMN table_calls text NOT NULL DEFAULT 'split' "
        "CHECK (table_calls IN ('split', 'merged'))"
    )
    op.execute(
        "ALTER TABLE ops.job_step ADD COLUMN table_calls text "
        "CHECK (table_calls IN ('split', 'merged'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ops.job_step DROP COLUMN table_calls")
    op.execute("ALTER TABLE ops.job DROP COLUMN table_calls")
