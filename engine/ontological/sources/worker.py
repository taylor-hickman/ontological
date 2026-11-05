"""The source executor's child process: one read-only source connection (ARCHITECTURE.md §5.6).

The engine side is `executor.py`. Messages cross a `multiprocessing` pipe. The child sends
`("ready", None)` or `("failed", message)` once, then the engine sends `(op_id, op, args)` and the
child answers `(op_id, "ok", payload)`, `(op_id, "error", message)` or `(op_id, "interrupted",
None)`. `("cancel", op_id)` asks it to `interrupt()` that operation. Everything sent is plain
Python data; the engine builds its own types from it.

The main thread runs operations one at a time, so the reader thread is free to take a cancel
while a query runs. When the pipe closes, the engine has gone, and the child exits at once,
whatever it is running.
"""

from __future__ import annotations

import contextlib
import os
import queue
import signal
import sqlite3
import threading
from collections.abc import Generator
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from urllib.parse import quote

import duckdb

ALIAS = "src"

Record = dict[str, Any]


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Refused(Exception):
    """The SQL isn't one read-only SELECT."""


class Session:
    """A DuckDB connection with the source attached read-only, and nothing else reachable."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"SQLite database not found: {path}")
        self.path = path
        with self._schema() as schema:  # fails here if the file isn't a SQLite database
            schema.execute("SELECT count(*) FROM sqlite_master").fetchall()
        self.con = duckdb.connect(":memory:")
        literal = str(path).replace("'", "''")
        self.con.execute(f"ATTACH '{literal}' AS {ALIAS} (TYPE sqlite, READ_ONLY)")
        self.con.execute(f"USE {ALIAS}")
        # The source is attached, so nothing else on disk needs reading: no other file, no
        # COPY ... TO, no ATTACH. Locking the configuration keeps a query from turning it back on.
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")
        self._lock = threading.Lock()
        self._current: int | None = None
        self._cancelled: int | None = None

    # Cancel

    def begin(self, op_id: int) -> None:
        with self._lock:
            self._current = op_id

    def end(self) -> None:
        with self._lock:
            self._current = None

    def interrupt(self, op_id: int) -> None:
        """Stop `op_id` if it is the one running. An `interrupt()` with nothing running is a
        no-op in DuckDB, so a cancel that lands between statements is kept for `check()`."""
        with self._lock:
            if self._current == op_id:
                self._cancelled = op_id
                self.con.interrupt()

    def check(self) -> None:
        with self._lock:
            if self._cancelled is not None and self._cancelled == self._current:
                raise duckdb.InterruptException("cancelled between statements")

    def _query(self, sql: str) -> list[tuple[Any, ...]]:
        self.check()
        return self.con.execute(sql).fetchall()

    # Operations

    def rows(self, sql: str, max_rows: int) -> dict[str, Any]:
        """One SELECT, keeping at most `max_rows` rows and whether there were more."""
        self.check()
        try:
            statements = self.con.extract_statements(sql)
        except duckdb.Error as e:
            raise Refused(str(e)) from e
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise Refused("only a single read-only SELECT statement can run on a source")
        result = self.con.execute(sql)
        description = result.description or []
        rows = result.fetchmany(max_rows + 1)
        return {
            "columns": [d[0] for d in description],
            "types": [str(d[1]) for d in description],
            "rows": rows[:max_rows],
            "truncated": len(rows) > max_rows,
        }

    def extract(self) -> dict[str, Any]:
        """The catalog in the shape upstream's `catalog/extract.py` reads from a connector.

        Relations, view definitions and keys come from SQLite's own schema, as upstream's SQLite
        connector reads them: DuckDB's scanner lists no foreign keys, and one view it can't
        translate makes `information_schema.tables` fail for the whole file. Column types are
        DuckDB's, since DuckDB is what runs the queries. A view DuckDB can't describe can't be
        queried either, so it is left out of `tables` and `views` and listed in `skipped`, which
        keeps upstream's rule that every listed relation has columns."""
        with self._schema() as schema:
            listed = schema.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables: list[Record] = []
            columns: list[Record] = []
            views: list[Record] = []
            skipped: list[Record] = []
            for name, kind, sql in listed:
                try:
                    described = self._query(f"DESCRIBE {ALIAS}.main.{quote_ident(name)}")
                except duckdb.InterruptException:
                    raise
                except duckdb.Error as e:
                    skipped.append({"table_schema": "main", "table_name": name, "error": str(e)})
                    continue
                tables.append(
                    {
                        "table_schema": "main",
                        "table_name": name,
                        "table_type": "view" if kind == "view" else "base table",
                    }
                )
                columns += [
                    {
                        "table_schema": "main",
                        "table_name": name,
                        "column_name": column,
                        "data_type": data_type,
                        "is_nullable": null == "YES",
                        "ordinal_position": position,
                    }
                    for position, (column, data_type, null, *_) in enumerate(described, 1)
                ]
                if kind == "view":
                    views.append(
                        {"table_schema": "main", "table_name": name, "view_definition": sql}
                    )
            self.check()
            base = [t["table_name"] for t in tables if t["table_type"] == "base table"]
            pks, fks = self._keys(schema, base)
        return {
            "database_name": self.path.stem,
            "tables": tables,
            "columns": columns,
            "views": views,
            "pks": pks,
            "fks": fks,
            "skipped": skipped,
        }

    @contextlib.contextmanager
    def _schema(self) -> Generator[sqlite3.Connection]:
        """The file through SQLite itself, read-only, for its schema."""
        con = sqlite3.connect(f"file:{quote(str(self.path))}?mode=ro", uri=True)
        try:
            yield con
        finally:
            con.close()

    @staticmethod
    def _keys(con: sqlite3.Connection, tables: list[str]) -> tuple[list[Record], list[Record]]:
        """Primary and foreign keys, from SQLite's pragmas."""
        pk_columns: dict[str, list[str]] = {}
        pks: list[Record] = []
        for table in tables:
            info = con.execute("SELECT name, pk FROM pragma_table_info(?)", (table,))
            key = sorted((pk, name) for name, pk in info.fetchall() if pk > 0)
            pk_columns[table] = [name for _, name in key]
            pks += [
                {
                    "table_schema": "main",
                    "table_name": table,
                    "column_name": name,
                    "ordinal_position": pk,
                }
                for pk, name in key
            ]
        fks: list[Record] = []
        for table in tables:
            listed: list[tuple[int, str, str, str | None]] = con.execute(
                'SELECT seq, "table", "from", "to" FROM pragma_foreign_key_list(?) '
                "ORDER BY id, seq",
                (table,),
            ).fetchall()
            for seq, ref_table, from_col, referenced in listed:
                if referenced is None:  # REFERENCES t with no column means t's primary key
                    ref_key: list[str] = pk_columns.get(ref_table, [])
                    referenced = ref_key[seq] if seq < len(ref_key) else None
                fks.append(
                    {
                        "table_schema": "main",
                        "table_name": table,
                        "column_name": from_col,
                        "referenced_schema": "main",
                        "referenced_table": ref_table,
                        "referenced_column": referenced,
                    }
                )
        return pks, fks

    def run(self, op: str, args: tuple[Any, ...]) -> Any:
        if op == "rows":
            return self.rows(*args)
        if op == "extract":
            return self.extract()
        raise ValueError(f"unknown operation {op!r}")


def serve(conn: Connection, path: str) -> None:
    """The child's entry point."""
    # Ctrl-C in the engine's terminal reaches its whole process group. The engine decides when
    # an executor stops, so the child leaves SIGINT to it.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        session = Session(Path(path))
    except Exception as e:
        conn.send(("failed", f"{type(e).__name__}: {e}"))
        return
    conn.send(("ready", None))

    requests: queue.Queue[tuple[int, str, tuple[Any, ...]]] = queue.Queue()

    def read() -> None:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                os._exit(0)  # the engine has gone; there is nothing to save
            if msg[0] == "cancel":
                session.interrupt(msg[1])
            else:
                requests.put(msg)

    threading.Thread(target=read, daemon=True).start()
    while True:
        op_id, op, args = requests.get()
        session.begin(op_id)
        try:
            reply = (op_id, "ok", session.run(op, args))
        except duckdb.InterruptException:
            reply = (op_id, "interrupted", None)
        except Exception as e:  # a query error, a refusal or a bug: the child carries on
            reply = (op_id, "error", str(e))
        finally:
            session.end()
        conn.send(reply)
