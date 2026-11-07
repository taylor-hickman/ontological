"""Stand-ins tests share: an embedder, a keychain, a Claude gateway for compile, a clock, and
sources for the answer gate's probes."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import duckdb
from keyring.backend import KeyringBackend
from langchain_core.embeddings import Embeddings
from pydantic import BaseModel

from ontological.answer.gate import KeyColumns
from ontological.llm.calls import Outcome
from ontological.llm.gateway import GatewayStatus, State
from ontological.sources.executor import Rows, SourceQueryError
from ontological.sources.session import Refused, Session


class HashEmbeddings(Embeddings):
    """Deterministic unit vectors from a text's hash, `dim` wide. The local embedder is M1.5."""

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [b - 127.5 for b in digest[: self.dim]]
        norm = sum(v * v for v in raw) ** 0.5
        return [v / norm for v in raw]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class MemoryKeyring(KeyringBackend):
    """A keychain in a dict, so no test touches the real one."""

    priority: ClassVar[float] = 1  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.items[(service, username)]


@dataclass(frozen=True)
class StructuredCall:
    task: str
    system: str
    user: str
    schema: type[BaseModel]
    prompt_version: str


@dataclass(frozen=True)
class NoAnswer:
    """What a scripted call returns when Claude didn't answer: the call's outcome, and the state
    the gateway is left in, with the `resets_at` of a rejected rate-limit event when one came."""

    outcome: Outcome = "limited"
    state: State = "limited"
    resets_at: datetime | None = None


# The model's JSON, None for an answer that failed validation, or NoAnswer
Answer = Callable[[str, str], dict[str, Any] | NoAnswer | None]


@dataclass
class ScriptedGateway:
    """`ClaudeGateway.structured()` for compile tests: `answer(task, user)` gives the model's
    JSON, None for an invalid answer, or `NoAnswer`. Records every call, and the most that were
    in flight at once."""

    answer: Answer
    calls: list[StructuredCall] = field(default_factory=list[StructuredCall])
    in_flight: int = 0
    peak: int = 0
    last_outcome: Outcome | None = None
    state: State = "ready"
    resets_at: datetime | None = None

    @property
    def status(self) -> GatewayStatus:
        return GatewayStatus(
            state=self.state,
            cli_path=None,
            installed=self.state != "not_installed",
            version=None,
            logged_in=self.state != "needs_login",
            rate_limit=None,
            limited_until=self.resets_at if self.state == "limited" else None,
            error=None if self.state == "ready" else self.state,
        )

    async def structured[M: BaseModel](
        self,
        task: str,
        system: str,
        user: str,
        schema: type[M],
        cache_key_extra: str = "",
        *,
        prompt_version: str = "",
    ) -> M | None:
        self.calls.append(StructuredCall(task, system, user, schema, prompt_version))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0)  # let anything else that could run, run
            output = self.answer(task, user)
        finally:
            self.in_flight -= 1
        if isinstance(output, NoAnswer):
            self.last_outcome, self.state = output.outcome, output.state
            self.resets_at = output.resets_at
            return None
        self.state, self.resets_at = "ready", None
        self.last_outcome = "invalid" if output is None else "ok"
        return None if output is None else schema.model_validate(output)

    def tasks(self) -> list[str]:
        return [c.task for c in self.calls]


T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@dataclass
class FakeClock:
    """A wall clock that `sleep` moves on at once. Records each sleep, and calls `on_sleep` with
    its length first, so a test can look at what a paused job left behind."""

    at: datetime = T0
    sleeps: list[float] = field(default_factory=list[float])
    on_sleep: Callable[[float], None] = lambda _: None

    def now(self) -> datetime:
        return self.at

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.on_sleep(seconds)
        self.at += timedelta(seconds=seconds)
        await asyncio.sleep(0)


def frame(**columns: list[Any]) -> Rows:
    """A probe's result, column by column: `frame(value=["a", "b"])`."""
    names = list(columns)
    values = list(columns.values())
    count = len(values[0]) if values else 0
    return Rows(
        names, ["VARCHAR"] * len(names), [tuple(v[i] for v in values) for i in range(count)], False
    )


class ScriptedProber:
    """A source whose probes answer with `results`, in order, and record their SQL."""

    def __init__(self, results: list[Rows]) -> None:
        self.results = list(results)
        self.seen: list[str] = []

    def probe(self, sql: str, *, max_rows: int) -> Rows:
        self.seen.append(sql)
        return self.results.pop(0)


class DuckProber:
    """A source session's `rows` on an in-memory DuckDB that `setup` fills, in this process:
    the probes run the SQL the executor's child would run."""

    def __init__(self, *setup: str) -> None:
        self.session = Session()
        for statement in setup:
            self.session.con.execute(statement)
        self.seen: list[str] = []

    def probe(self, sql: str, *, max_rows: int) -> Rows:
        self.seen.append(sql)
        try:
            return Rows(**self.session.rows(sql, max_rows))
        except (duckdb.Error, Refused) as e:
            raise SourceQueryError(str(e)) from e


Hop = dict[str, str]


def hop(source: str, target: str) -> Hop:
    """A semantic-FK hop from `source` (table.column) to `target`."""
    (st, sc), (tt, tc) = source.split("."), target.split(".")
    return {
        "source_schema": "public",
        "source_table": st,
        "source_column": sc,
        "target_schema": "public",
        "target_table": tt,
        "target_column": tc,
    }


@dataclass
class FakeGraph:
    """The semantic-FK graph: column ids are `table.column`, and `paths` holds the hops
    `find_join_path` returns from one column id to another."""

    paths: dict[tuple[str, str], list[Hop]] = field(
        default_factory=dict[tuple[str, str], list[Hop]]
    )
    fk_columns: set[str] = field(default_factory=set[str])
    hubs: dict[frozenset[str], dict[str, str]] = field(
        default_factory=dict[frozenset[str], dict[str, str]]
    )
    keys: dict[str, KeyColumns] = field(default_factory=dict[str, KeyColumns])
    asked: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def column_id(self, table: str, column: str) -> str | None:
        return f"{table}.{column}"

    def join_path(self, col_a_id: str, col_b_id: str) -> list[dict[str, Any]]:
        self.asked.append((col_a_id, col_b_id))
        return list(self.paths.get((col_a_id, col_b_id), []))

    def in_semantic_fk(self, col_id: str) -> bool:
        return col_id in self.fk_columns

    def shared_hub(self, col_a_id: str, col_b_id: str) -> dict[str, Any]:
        return dict(self.hubs.get(frozenset((col_a_id, col_b_id)), {}))

    def key_columns(self, table: str) -> KeyColumns:
        return self.keys.get(table.lower(), KeyColumns())
