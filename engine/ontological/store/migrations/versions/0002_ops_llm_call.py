"""ops.llm_call: the Claude gateway's usage log and structured-call cache (ARCHITECTURE.md §5.2,
§6.4). One row per spawn of `claude`; see ontological/llm/calls.py.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ops.llm_call (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT now(),
            task text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('structured', 'text')),
            prompt_version text NOT NULL,
            cache_key text NOT NULL,
            model text NOT NULL,
            effort text NOT NULL,
            attempt smallint NOT NULL,
            outcome text NOT NULL CHECK (outcome IN ('ok', 'invalid', 'limited', 'error')),
            output jsonb,
            error text,
            resolved_model text,
            answered_model text,
            fallback boolean NOT NULL DEFAULT false,
            usage jsonb,
            cost_usd double precision,
            duration_ms integer,
            num_turns integer,
            session_id text
        )
        """
    )
    op.execute(
        "CREATE INDEX llm_call_cache ON ops.llm_call (cache_key, id DESC) "
        "WHERE kind = 'structured' AND outcome = 'ok' AND NOT fallback"
    )


def downgrade() -> None:
    op.execute("DROP TABLE ops.llm_call")
