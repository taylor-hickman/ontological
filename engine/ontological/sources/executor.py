"""Source executors: one read-only source connection in a process of its own (ARCHITECTURE.md
§5.5, §5.6).

A `SourceExecutor` starts its child as soon as it is made, so the source is attached before the
first operation. It runs one operation at a time: `extract`, `history`, `profile`, `run_sql` or
`probe`. A timeout or a `cancel()` from another thread first asks the child to `interrupt()` the
query. If the operation is still running `KILL_AFTER_S` later, the executor kills the child,
starts a new one and raises.
Results cross the pipe small: at most `MAX_PROFILE_ROWS` rows from a probe, and `MAX_ANSWER_ROWS`
from `run_sql`. `profile` samples a table in the child and returns only each column's top values,
uniqueness and date format.

Every v1 source goes through DuckDB: a DuckDB file, Parquet, CSV and JSON files, a SQLite file,
and Postgres and MySQL through DuckDB's scanners (`spec.py`). A server source's password is read
from the keychain each time a child starts, and handed to it over the spawn pipe. The child is
`worker.py`.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from typing import Any, Literal

from ontological.profile.sampling import TableProfile
from ontological.sources import worker
from ontological.sources.keychain import CredentialError, read_password
from ontological.sources.spec import (
    DuckDBSource,
    FileSource,
    FileTable,
    MySQLSource,
    PostgresSource,
    Source,
    SqliteSource,
)

__all__ = [
    "MAX_ANSWER_ROWS",
    "MAX_PROFILE_ROWS",
    "Cancelled",
    "Catalog",
    "DuckDBSource",
    "ExecutorDied",
    "FileSource",
    "FileTable",
    "History",
    "MySQLSource",
    "PostgresSource",
    "Rows",
    "Source",
    "SourceError",
    "SourceExecutor",
    "SourceQueryError",
    "SqliteSource",
    "StatementTimeout",
    "TableProfile",
]

MAX_ANSWER_ROWS = 200
MAX_PROFILE_ROWS = 1000
KILL_AFTER_S = 2.0
RUN_SQL_TIMEOUT_S = 60.0
PROBE_TIMEOUT_S = 5.0
PROFILE_TIMEOUT_S = 120.0  # upstream's cap on one profiling query, here on a table's profile
START_TIMEOUT_S = 30.0
HISTORY_HOURS = 24  # upstream's `get_queries` window, and its scheduler's period
_POLL_S = 0.05  # how often a waiting operation looks for a cancel()

Record = dict[str, Any]


class SourceError(Exception):
    """The source can't be used: it failed to open, or the executor is closed."""


class SourceQueryError(SourceError):
    """The source refused or failed the SQL. The message is DuckDB's, for the agent to read."""


class Cancelled(SourceError):
    """`cancel()` stopped the operation."""


class StatementTimeout(SourceError, TimeoutError):
    """The operation outlived its `timeout_s` and was stopped."""


class ExecutorDied(SourceError):
    """The child exited in the middle of an operation. A new one has been started."""


class _Unstarted(Exception):
    """The child exited before it started the operation."""


@dataclass(frozen=True)
class Rows:
    columns: list[str]
    types: list[str]  # DuckDB's type names, one per column
    rows: list[tuple[Any, ...]]
    truncated: bool  # the query had more rows than were kept


@dataclass(frozen=True)
class Catalog:
    """What a source holds, as lists of records with the columns upstream's connectors return
    (`connectors/base.py::SQLDatabase`), so ingest can build its DataFrames from them."""

    database_name: str
    tables: list[Record]  # table_schema, table_name, table_type: "base table", "view" or
    # "materialized view"
    columns: list[Record]  # table_schema, table_name, column_name, data_type, is_nullable,
    # ordinal_position
    views: list[Record]  # table_schema, table_name, view_definition
    pks: list[Record]  # table_schema, table_name, column_name, ordinal_position
    fks: list[Record]  # table_schema, table_name, column_name, referenced_schema,
    # referenced_table, referenced_column
    skipped: list[Record]  # table_schema, table_name, error: relations DuckDB can't read


@dataclass(frozen=True)
class History:
    """A source's recent statements, as upstream's `get_queries` returns them."""

    dialects: list[str]  # sqlglot's names for the dialects the statements are written in
    rows: list[Record]  # query_text, end_time (UTC), count


class SourceExecutor:
    def __init__(self, source: Source, *, kill_after_s: float = KILL_AFTER_S) -> None:
        self.source = source
        self.kill_after_s = kill_after_s
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()  # one operation at a time
        self._cancel = threading.Event()
        self._busy = False
        self._closed = False
        self._op_id = 0
        self._proc: BaseProcess | None = None
        self._conn: Connection | None = None
        self._ready = False
        with contextlib.suppress(CredentialError):  # the first operation reports it
            self._start()

    def __enter__(self) -> SourceExecutor:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def busy(self) -> bool:
        """An operation is in flight."""
        return self._busy

    # Operations

    def wait_ready(self) -> None:
        """Until the child has attached the source. Raises SourceError if it couldn't."""
        with self._lock:
            self._ensure_ready()

    def extract(self, *, timeout_s: float | None = None) -> Catalog:
        catalog = self._call("extract", (), timeout_s)
        return Catalog(**catalog)

    def history(self, *, hours: int = HISTORY_HOURS, timeout_s: float | None = None) -> History:
        """The statements the source ran in the last `hours` hours. A source that keeps no
        history, which is every source but Postgres, returns none."""
        return History(**self._call("history", (hours,), timeout_s))

    def profile(
        self,
        schema: str,
        table: str,
        columns: list[Record],
        *,
        probe_distinct_values: bool = True,
        timeout_s: float = PROFILE_TIMEOUT_S,
    ) -> TableProfile:
        """Profile one table from a sample of at most `MAX_PROFILE_ROWS` rows, taken and read in
        the child (`ontological/profile/sampling.py`). `columns` are the catalog's `name` and
        `data_type` for each. A row sample the source refuses raises `SourceQueryError`."""
        args = (schema, table, columns, probe_distinct_values)
        return self._call("profile", args, timeout_s)

    def run_sql(self, sql: str, *, timeout_s: float = RUN_SQL_TIMEOUT_S) -> Rows:
        """The answer agent's query: one SELECT, at most `MAX_ANSWER_ROWS` rows."""
        return Rows(**self._call("rows", (sql, MAX_ANSWER_ROWS), timeout_s))

    def probe(
        self, sql: str, *, max_rows: int = MAX_ANSWER_ROWS, timeout_s: float = PROBE_TIMEOUT_S
    ) -> Rows:
        """One capped, read-only query: the gate's probes, empty-result handling, profiling and
        the semantic-FK value-overlap check. `max_rows` is at most `MAX_PROFILE_ROWS`."""
        if not 0 < max_rows <= MAX_PROFILE_ROWS:
            raise ValueError(f"max_rows must be between 1 and {MAX_PROFILE_ROWS}, not {max_rows}")
        return Rows(**self._call("rows", (sql, max_rows), timeout_s))

    def fetch(self, sql: str, *, max_rows: int, timeout_s: float = RUN_SQL_TIMEOUT_S) -> Rows:
        """One read-only query with a caller's row cap, for the eval's verifier, which compares
        the whole result with the gold query's (BUILD_PLAN.md, Scoring and runs)."""
        if max_rows < 1:
            raise ValueError(f"max_rows must be at least 1, not {max_rows}")
        return Rows(**self._call("rows", (sql, max_rows), timeout_s))

    def cancel(self) -> None:
        """Stop the operation in flight, from any thread. Does nothing when there is none."""
        if self._busy:
            self._cancel.set()

    def close(self) -> None:
        """Stop the child. An operation in flight is cancelled first."""
        self._closed = True
        self.cancel()
        with self._lock:
            self._stop()

    # The child

    def _start(self) -> None:
        """Start a child. The spawn pipe carries its arguments, so the password never reaches
        argv or the disk."""
        password = None
        if isinstance(self.source, PostgresSource | MySQLSource) and self.source.password:
            password = read_password(self.source.password)
        parent, child = self._ctx.Pipe()
        proc = self._ctx.Process(
            target=worker.serve,
            args=(child, self.source, password),
            name=f"source-executor:{self.source.database_name}",
            daemon=True,
        )
        proc.start()
        child.close()  # so the parent sees EOF when the child exits
        self._proc, self._conn, self._ready = proc, parent, False

    def _stop(self, *, kill: bool = False) -> None:
        """Stop the child: close the pipe, which the child exits on, or kill it outright."""
        proc, conn = self._proc, self._conn
        self._proc, self._conn, self._ready = None, None, False
        if conn is not None:
            conn.close()
        if proc is None:
            return
        if not kill:
            proc.join(self.kill_after_s)
        if proc.is_alive():
            proc.kill()
        proc.join()

    def _restart(self) -> None:
        self._stop(kill=True)
        self._start()

    def _ensure_ready(self) -> None:
        if self._closed:
            raise SourceError("the source executor is closed")
        if self._proc is not None and not self._proc.is_alive():
            self._stop(kill=True)  # it died between operations: start a new one
        if self._proc is None:
            try:
                self._start()
            except CredentialError as e:
                raise SourceError(f"{self.source.label}: {e}") from e
        if self._ready:
            return
        assert self._proc is not None and self._conn is not None
        if not wait([self._conn, self._proc.sentinel], START_TIMEOUT_S):
            self._stop(kill=True)
            raise SourceError(f"the source executor didn't start in {START_TIMEOUT_S:g}s")
        try:
            kind, detail = self._conn.recv()
        except (EOFError, OSError):
            kind, detail = "failed", "the source executor exited while it started"
        if kind != "ready":
            self._stop(kill=True)  # the next operation tries again with a new child
            raise SourceError(f"{self.source.label}: {detail}")
        self._ready = True

    def _call(self, op: str, args: tuple[Any, ...], timeout_s: float | None) -> Any:
        with self._lock:
            self._ensure_ready()
            self._cancel.clear()
            self._busy = True
            try:
                try:
                    return self._send(op, args, timeout_s)
                except _Unstarted:
                    # The child died while idle, though `_ensure_ready` saw it alive: on Linux a
                    # killed process whose threads are still exiting can't be reaped yet, and
                    # its end of the pipe stays open. It never started the operation, so a new
                    # child can take it.
                    self._stop(kill=True)
                    self._ensure_ready()
                    try:
                        return self._send(op, args, timeout_s)
                    except _Unstarted:
                        raise self._died() from None
            finally:
                self._busy = False

    def _send(self, op: str, args: tuple[Any, ...], timeout_s: float | None) -> Any:
        assert self._conn is not None
        self._op_id += 1
        try:
            self._conn.send((self._op_id, op, args))
        except OSError:
            raise _Unstarted from None
        return self._await(self._op_id, timeout_s)

    def _await(self, op_id: int, timeout_s: float | None) -> Any:
        assert self._proc is not None and self._conn is not None
        conn, sentinel = self._conn, self._proc.sentinel
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        why: Literal["cancel", "timeout"] | None = None
        kill_at = 0.0
        started = False
        while True:
            if wait([conn, sentinel], _POLL_S):
                try:
                    reply_id, kind, payload = conn.recv()
                except (EOFError, OSError):
                    if not started:
                        raise _Unstarted from None
                    raise self._died() from None
                if reply_id != op_id:
                    continue
                if kind == "started":
                    started = True
                    continue
                if kind == "ok":
                    return payload
                if kind == "error":
                    raise SourceQueryError(payload)
                raise self._stopped(why or "cancel", timeout_s)
            now = time.monotonic()
            if why is None:
                if self._cancel.is_set():
                    why = "cancel"
                elif deadline is not None and now >= deadline:
                    why = "timeout"
                if why is not None:
                    try:
                        conn.send(("cancel", op_id))
                    except OSError:
                        raise self._died() from None
                    kill_at = now + self.kill_after_s
            elif now >= kill_at:
                self._restart()
                raise self._stopped(why, timeout_s)

    def _died(self) -> ExecutorDied:
        """The child has gone mid-operation: start a new one, and the error to raise."""
        self._restart()
        return ExecutorDied(f"the source executor for {self.source.label} exited")

    @staticmethod
    def _stopped(why: Literal["cancel", "timeout"], timeout_s: float | None) -> SourceError:
        if why == "timeout":
            return StatementTimeout(f"the query ran longer than its {timeout_s:g}s timeout")
        return Cancelled("the operation was cancelled")
