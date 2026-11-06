"""ops.llm_call: every Claude call's usage log, and the structured-call cache (ARCHITECTURE.md
§5.2, §6.4).

Each spawn writes one row. A structured call's key is `cache_key(...)`; a later call with the same
key is answered from the newest row that holds a valid answer from the route's own model, and
writes a `cached` row of its own with no output, so a job's log shows its cache hits. Text calls
are logged but never served from the cache.

A row names the job and table it was for when the caller set them with `call_scope`, as compile
does for each table; the Jobs screen reads a job's calls by them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel
from sqlalchemy import create_engine, text

Kind = Literal["structured", "text"]
# ok: an answer. invalid: structured output missing or failing validation (§6.2's retry case).
# limited: rate limit or overload (§6.5). error: anything else, such as a failed login.
Outcome = Literal["ok", "invalid", "limited", "error"]


def cache_key(
    *,
    task: str,
    prompt_version: str,
    model: str,
    effort: str,
    system: str,
    user: str,
    schema: type[BaseModel] | None,
    extra: str = "",
) -> str:
    """sha256 of the parts §6.4 names, plus the caller's extra. `model` is the route's string as
    written, so with an alias a model release doesn't invalidate the cache."""
    parts = [
        task,
        prompt_version,
        model,
        effort,
        system,
        user,
        None if schema is None else schema.model_json_schema(),
        extra,
    ]
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class CallScope:
    """What a call is for: the job and the table, for the job's call log."""

    job_id: int | None = None
    table_name: str | None = None


_scope: ContextVar[CallScope | None] = ContextVar("call_scope", default=None)


@contextmanager
def call_scope(job_id: int | None, table_name: str | None) -> Generator[None]:
    """Calls made inside, in this task or a thread it starts, are logged under this job and
    table."""
    token = _scope.set(CallScope(job_id, table_name))
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> CallScope:
    return _scope.get() or CallScope()


@dataclass(frozen=True)
class LlmCall:
    """One row of ops.llm_call: one spawn of `claude`."""

    task: str
    kind: Kind
    prompt_version: str
    cache_key: str
    model: str  # the route's model as written
    effort: str
    attempt: int  # 1, or 2 for §6.2's retry
    outcome: Outcome
    output: Any = None  # the validated structured output, or the text
    error: str | None = None
    resolved_model: str | None = None  # the init message's model: what the alias resolved to
    answered_model: str | None = None  # the AssistantMessage model, the fallback's if it ran
    fallback: bool = False  # the fallback model answered (a system/model_fallback message)
    usage: dict[str, Any] | None = None  # ResultMessage.model_usage, side calls included
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    session_id: str | None = None
    cached: bool = False  # answered from the cache: no spawn, and no output of its own
    job_id: int | None = None
    table_name: str | None = None

    @property
    def cacheable(self) -> bool:
        """A valid structured answer from the route's own model. A fallback answer isn't served
        again, so the next run asks the route's model."""
        return (
            self.kind == "structured"
            and self.outcome == "ok"
            and not self.fallback
            and not self.cached
        )


class CallLog(Protocol):
    """Where the gateway logs calls and looks up cached answers. Calls may block; the gateway
    runs them off the event loop."""

    def lookup(self, cache_key: str) -> Any | None:
        """The output of the newest cacheable row with this key, or None."""
        ...

    def record(self, call: LlmCall) -> None: ...


_INSERT = text(
    """
    INSERT INTO ops.llm_call (
        task, kind, prompt_version, cache_key, model, effort, attempt, outcome, output, error,
        resolved_model, answered_model, fallback, usage, cost_usd, duration_ms, num_turns,
        session_id, cached, job_id, table_name
    ) VALUES (
        :task, :kind, :prompt_version, :cache_key, :model, :effort, :attempt, :outcome,
        CAST(:output AS jsonb), :error, :resolved_model, :answered_model, :fallback,
        CAST(:usage AS jsonb), :cost_usd, :duration_ms, :num_turns, :session_id, :cached,
        :job_id, :table_name
    )
    """
)

_LOOKUP = text(
    """
    SELECT output FROM ops.llm_call
    WHERE cache_key = :cache_key AND kind = 'structured' AND outcome = 'ok' AND NOT fallback
        AND NOT cached
    ORDER BY id DESC
    LIMIT 1
    """
)


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


class PgCallLog:
    """ops.llm_call in the store."""

    def __init__(self, url: str) -> None:
        self._engine = create_engine(url)

    def lookup(self, cache_key: str) -> Any | None:
        with self._engine.connect() as conn:
            row = conn.execute(_LOOKUP, {"cache_key": cache_key}).first()
        return None if row is None else row[0]

    def record(self, call: LlmCall) -> None:
        params: dict[str, Any] = {
            "task": call.task,
            "kind": call.kind,
            "prompt_version": call.prompt_version,
            "cache_key": call.cache_key,
            "model": call.model,
            "effort": call.effort,
            "attempt": call.attempt,
            "outcome": call.outcome,
            "output": _json(call.output),
            "error": call.error,
            "resolved_model": call.resolved_model,
            "answered_model": call.answered_model,
            "fallback": call.fallback,
            "usage": _json(call.usage),
            "cost_usd": call.cost_usd,
            "duration_ms": call.duration_ms,
            "num_turns": call.num_turns,
            "session_id": call.session_id,
            "cached": call.cached,
            "job_id": call.job_id,
            "table_name": call.table_name,
        }
        with self._engine.begin() as conn:
            conn.execute(_INSERT, params)

    def close(self) -> None:
        self._engine.dispose()
