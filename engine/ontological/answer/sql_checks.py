"""`validate_sql` and `run_validated_sql` for a caller with no agent session (ARCHITECTURE.md
§9.2, BUILD_PLAN.md M6.2): what `POST /sql/validate` and `POST /sql/run` run, so a Claude Code
session can write the SQL itself and still pass it through Ontological's gate.

`validate` runs the whole §8.2 gate (`ontological.answer.gate.check_sql`) on SQL for a database,
as the agent's `validate_sql` does, and keeps SQL that passes under a new `validation_id`. `run`
runs SQL by that id, and only SQL that passed, so nothing reaches the source that the gate
hasn't seen. An empty result is its own outcome, as in the agent's `run_sql`: it reruns the
literal check knowing the result is empty, probes the query without its LIKE filters, and says
what to recheck.

There is no plan and no question, so neither precondition applies, and there is no cap on calls.
Each call gets `MAX_PROBES` probes of its own. The soft checks block unless the caller asks for
them as warnings, which the MCP shim does after `SOFT_CHECKS_UNTIL` failures in a row, as the
agent's session does after that many calls.

The ids live in memory, the latest `MAX_KEPT`; an engine that restarts forgets them, and the
caller validates again.
"""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from ontological.answer.gate import GateResult, KeyColumns, Probing, check_sql
from ontological.answer.join_check import JoinGraph
from ontological.answer.probe import ProbeExecutor
from ontological.answer.tools import (
    Runners,
    SqlRunner,
    cell,
    empty_message,
    empty_repairs,
    rows_without_like,
)
from ontological.catalog.parsers.catalog_index import CatalogIndex
from ontological.sources.executor import SourceError

MAX_KEPT = 500  # validations kept for run; the oldest is forgotten first


class GateReads(Protocol):
    """What the gate reads from the store about a database (`answer.ontology.StoreCatalog`)."""

    def catalog(self, database: str) -> CatalogIndex: ...

    def key_columns(self, database: str, table: str) -> KeyColumns: ...

    def join_graph(self, database: str) -> JoinGraph: ...


class Validated(BaseModel):
    ok: bool
    validation_id: str | None  # None when the SQL has issues
    database: str
    sql: str
    issues: list[str]  # what to fix; empty when it passed
    warnings: list[str]  # soft checks that would have failed it, when asked for as warnings
    tables: list[str]  # schema.Table, the tables it reads
    columns: list[str]  # Table.Column, the columns it uses


class WithoutLike(BaseModel):
    """What an empty query returns with its LIKE and ILIKE filters dropped."""

    dropped: list[str]
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


class RanSql(BaseModel):
    validation_id: str
    database: str
    sql: str
    outcome: Literal["rows", "empty"]
    columns: list[str]
    types: list[str]
    rows: list[list[Any]]  # at most the executor's row cap
    row_count: int
    truncated: bool
    repairs: str = ""  # for an empty result: the literal fixes found
    without_like: WithoutLike | None = None  # for an empty result emptied by its LIKE filters
    note: str | None = None  # for an empty result: what to recheck before taking it as the answer


class NoSuchValidation(Exception):
    """A validation_id `validate` didn't issue, or has forgotten."""


class RunFailed(Exception):
    """The source couldn't run SQL that passed the gate; the message says why."""


@dataclass(frozen=True)
class _Passed:
    database: str
    sql: str
    gate: GateResult


class SqlChecks:
    def __init__(self, reads: GateReads, runners: Runners) -> None:
        """`runners` opens a database's source, raising `NoSource` when it has none. Both
        methods block: they read the store and the source."""
        self.reads = reads
        self.runners = runners
        self._passed: OrderedDict[str, _Passed] = OrderedDict()
        self._lock = threading.Lock()

    def validate(self, database: str, sql: str, *, soft_as_warnings: bool = False) -> Validated:
        probing = Probing(ProbeExecutor(self._runner(database)), self.reads.join_graph(database))
        gate = check_sql(
            sql,
            self.reads.catalog(database),
            key_columns=lambda table: self.reads.key_columns(database, table),
            probing=probing,
            soft_as_warnings=soft_as_warnings,
        )
        validation_id = None
        if gate.ok:
            validation_id = f"val-{secrets.token_hex(6)}"
            with self._lock:
                self._passed[validation_id] = _Passed(database, sql, gate)
                while len(self._passed) > MAX_KEPT:
                    self._passed.popitem(last=False)
        return Validated(
            ok=gate.ok,
            validation_id=validation_id,
            database=database,
            sql=sql,
            issues=gate.issues,
            warnings=gate.warnings,
            tables=gate.tables,
            columns=gate.columns,
        )

    def run(self, validation_id: str) -> RanSql:
        with self._lock:
            passed = self._passed.get(validation_id)
        if passed is None:
            raise NoSuchValidation(validation_id)
        runner = self._runner(passed.database)
        try:
            rows = runner.run_sql(passed.sql)
        except SourceError as e:
            raise RunFailed(str(e)) from e
        ran = RanSql(
            validation_id=validation_id,
            database=passed.database,
            sql=passed.sql,
            outcome="rows" if rows.rows else "empty",
            columns=rows.columns,
            types=rows.types,
            rows=[[cell(v) for v in row] for row in rows.rows],
            row_count=len(rows.rows),
            truncated=rows.truncated,
        )
        if rows.rows:
            return ran
        probing = Probing(ProbeExecutor(runner), self.reads.join_graph(passed.database))
        ran.repairs = empty_repairs(probing, passed.sql)
        without_like = rows_without_like(probing, passed.sql)
        ran.without_like = WithoutLike.model_validate(without_like) if without_like else None
        ran.note = empty_message(ran.model_dump())
        return ran

    def _runner(self, database: str) -> SqlRunner:
        """The database's source, opened once: concurrent calls share it."""
        with self._lock:
            return self.runners(database)
