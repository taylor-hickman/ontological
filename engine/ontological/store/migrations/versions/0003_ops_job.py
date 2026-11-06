"""ops.job and ops.job_step: background jobs and their resumable checkpoints (ARCHITECTURE.md
§5.2, §6.5). A compile is a job with one step per table; see ontological/jobs/store.py.

`table_id` names a catalog table without a foreign key: the catalog is the other chain's, and a
re-ingest may drop a table a finished job compiled.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ops.job (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            kind text NOT NULL CHECK (kind IN ('compile')),
            database_name text NOT NULL,
            state text NOT NULL CHECK (state IN ('running', 'paused', 'stopped', 'done')),
            paused_until timestamptz,
            error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            CHECK ((state = 'paused') = (paused_until IS NOT NULL))
        )
        """
    )
    # One unfinished job of a kind per database: the one the next run resumes
    op.execute(
        "CREATE UNIQUE INDEX job_open ON ops.job (kind, database_name) WHERE state <> 'done'"
    )
    op.execute(
        """
        CREATE TABLE ops.job_step (
            job_id bigint NOT NULL REFERENCES ops.job (id) ON DELETE CASCADE,
            position integer NOT NULL,
            table_id text NOT NULL,
            table_name text NOT NULL,
            pack integer NOT NULL,
            state text NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'done', 'skipped')),
            detail jsonb,
            finished_at timestamptz,
            PRIMARY KEY (job_id, position),
            UNIQUE (job_id, table_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ops.job_step")
    op.execute("DROP TABLE ops.job")
