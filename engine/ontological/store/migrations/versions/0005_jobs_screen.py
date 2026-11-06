"""What the Jobs screen reads (DESIGN.md §5.4, BUILD_PLAN.md M3.8).

ops.llm_call: `job_id`, `table_id` and `table_name` say which job and table a call was for, so the
screen can show a job's call log and each table's stages. `cached` marks a row the gateway wrote
for a cache hit: no spawn, no output, never served itself.

ops.job: an `ingest` kind for the staleness check's refreshes, with the tables a refresh found in
`result`; `dismissed_at` for a new-tables prompt the person put off; and a `held` state, for a
job the person paused, which waits until they resume it.

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
    op.execute(
        "ALTER TABLE ops.llm_call "
        "ADD COLUMN job_id bigint REFERENCES ops.job (id) ON DELETE SET NULL, "
        "ADD COLUMN table_id text, "
        "ADD COLUMN table_name text, "
        "ADD COLUMN cached boolean NOT NULL DEFAULT false"
    )
    op.execute("CREATE INDEX llm_call_job ON ops.llm_call (job_id, id) WHERE job_id IS NOT NULL")
    op.execute("DROP INDEX ops.llm_call_cache")
    op.execute(
        "CREATE INDEX llm_call_cache ON ops.llm_call (cache_key, id DESC) "
        "WHERE kind = 'structured' AND outcome = 'ok' AND NOT fallback AND NOT cached"
    )

    op.execute("ALTER TABLE ops.job DROP CONSTRAINT job_kind_check")
    op.execute(
        "ALTER TABLE ops.job ADD CONSTRAINT job_kind_check CHECK (kind IN ('compile', 'ingest'))"
    )
    op.execute("ALTER TABLE ops.job DROP CONSTRAINT job_state_check")
    op.execute(
        "ALTER TABLE ops.job ADD CONSTRAINT job_state_check "
        "CHECK (state IN ('running', 'paused', 'held', 'stopped', 'done'))"
    )
    op.execute("ALTER TABLE ops.job ADD COLUMN result jsonb, ADD COLUMN dismissed_at timestamptz")
    # A refresh that failed is recorded `stopped` and never resumed, so only compiles keep to one
    # unfinished job per database.
    op.execute("DROP INDEX ops.job_open")
    op.execute(
        "CREATE UNIQUE INDEX job_open ON ops.job (kind, database_name) "
        "WHERE state <> 'done' AND kind = 'compile'"
    )


def downgrade() -> None:
    op.execute("DELETE FROM ops.job WHERE kind = 'ingest'")
    op.execute("UPDATE ops.job SET state = 'stopped', error = 'paused by you' WHERE state = 'held'")
    op.execute("DROP INDEX ops.job_open")
    op.execute(
        "CREATE UNIQUE INDEX job_open ON ops.job (kind, database_name) WHERE state <> 'done'"
    )
    op.execute("ALTER TABLE ops.job DROP COLUMN result, DROP COLUMN dismissed_at")
    op.execute("ALTER TABLE ops.job DROP CONSTRAINT job_state_check")
    op.execute(
        "ALTER TABLE ops.job ADD CONSTRAINT job_state_check "
        "CHECK (state IN ('running', 'paused', 'stopped', 'done'))"
    )
    op.execute("ALTER TABLE ops.job DROP CONSTRAINT job_kind_check")
    op.execute("ALTER TABLE ops.job ADD CONSTRAINT job_kind_check CHECK (kind IN ('compile'))")
    op.execute("DELETE FROM ops.llm_call WHERE cached")
    op.execute("DROP INDEX ops.llm_call_cache")
    op.execute(
        "CREATE INDEX llm_call_cache ON ops.llm_call (cache_key, id DESC) "
        "WHERE kind = 'structured' AND outcome = 'ok' AND NOT fallback"
    )
    op.execute("DROP INDEX ops.llm_call_job")
    op.execute(
        "ALTER TABLE ops.llm_call DROP COLUMN job_id, DROP COLUMN table_id, "
        "DROP COLUMN table_name, DROP COLUMN cached"
    )
