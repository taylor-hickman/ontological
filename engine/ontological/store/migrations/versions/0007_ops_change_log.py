"""ops.change_log: every curation edit, field by field, for history and undo (ARCHITECTURE.md
§5.7); and `reembed`, the job a change of embedding model runs (§7.1). See
ontological/curate/edits.py and ontological/embed/reembed.py.

An edit is one or more rows sharing `edit`, written in the transaction that applies it. `old`
and `new` are JSON, since fields are text, arrays and booleans. An undo is an edit too, whose rows
name the edit they revert in `undoes`. `entity_id` names a Term or attribute without a foreign
key: the history outlives what it describes, and the store's semantic tables are the other
chain's.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE ops.change_log_edit_seq")
    op.execute(
        """
        CREATE TABLE ops.change_log (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            edit bigint NOT NULL,
            ts timestamptz NOT NULL DEFAULT now(),
            entity text NOT NULL CHECK (entity IN ('term', 'column_attribute')),
            entity_id text NOT NULL,
            field text NOT NULL,
            old jsonb NOT NULL,
            new jsonb NOT NULL,
            actor text NOT NULL CHECK (actor IN ('user', 'compile', 'import')),
            undoes bigint,
            UNIQUE (edit, entity, entity_id, field)
        )
        """
    )
    op.execute("CREATE INDEX change_log_entity ON ops.change_log (entity, entity_id, id)")
    op.execute("CREATE INDEX change_log_undoes ON ops.change_log (undoes) WHERE undoes IS NOT NULL")
    op.execute("ALTER TABLE ops.job DROP CONSTRAINT job_kind_check")
    op.execute(
        "ALTER TABLE ops.job ADD CONSTRAINT job_kind_check "
        "CHECK (kind IN ('compile', 'ingest', 'reembed'))"
    )
    # A re-embed resumes like a compile, so it keeps to one unfinished job per database too
    op.execute("DROP INDEX ops.job_open")
    op.execute(
        "CREATE UNIQUE INDEX job_open ON ops.job (kind, database_name) "
        "WHERE state <> 'done' AND kind IN ('compile', 'reembed')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM ops.job WHERE kind = 'reembed'")
    op.execute("DROP INDEX ops.job_open")
    op.execute(
        "CREATE UNIQUE INDEX job_open ON ops.job (kind, database_name) "
        "WHERE state <> 'done' AND kind = 'compile'"
    )
    op.execute("ALTER TABLE ops.job DROP CONSTRAINT job_kind_check")
    op.execute(
        "ALTER TABLE ops.job ADD CONSTRAINT job_kind_check CHECK (kind IN ('compile', 'ingest'))"
    )
    op.execute("DROP TABLE ops.change_log")
    op.execute("DROP SEQUENCE ops.change_log_edit_seq")
