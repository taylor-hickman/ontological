"""What every kind of source session shares, inside the executor's child (ARCHITECTURE.md §5.6).

A session is an in-memory DuckDB connection with one source attached and nothing else
reachable. Each kind of source subclasses `Session`: it attaches the source, calls
`lock_down()`, and reads its catalog in `extract()`. Running SQL, and cancelling it, are the
same for every kind.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import duckdb

ALIAS = "src"

Record = dict[str, Any]

BASE_TABLE = "base table"
VIEW = "view"
MATERIALIZED_VIEW = "materialized view"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class Refused(Exception):
    """The SQL isn't one read-only SELECT."""


@dataclass(frozen=True)
class Relation:
    schema: str
    name: str
    type: str  # BASE_TABLE, VIEW or MATERIALIZED_VIEW
    catalog: str = ALIAS

    @property
    def qualified(self) -> str:
        return ".".join(quote_ident(p) for p in (self.catalog, self.schema, self.name))


class Session:
    """The connection, `rows()`, cancel, and the catalog's shape. Subclasses attach a source
    and read its catalog."""

    database_name: str

    def __init__(self) -> None:
        self.con = duckdb.connect(":memory:")
        self._lock = threading.Lock()
        self._current: int | None = None
        self._cancelled: int | None = None

    def lock_down(self, *, paths: Iterable[str] = (), directories: Iterable[str] = ()) -> None:
        """Once the source is attached, nothing else needs reading: no other file, no COPY ...
        TO, no ATTACH. `paths` and `directories` are the files a file source's views read when
        they run. Locking the configuration keeps a query from turning any of it back on."""
        paths, directories = list(paths), list(directories)
        if paths:
            self.con.execute(f"SET allowed_paths = [{', '.join(map(quote_literal, paths))}]")
        if directories:
            listed = ", ".join(map(quote_literal, directories))
            self.con.execute(f"SET allowed_directories = [{listed}]")
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")

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
            if self._current != op_id:
                return
            self._cancelled = op_id
            self.con.interrupt()
        # It can block on the network, and the reader thread has to stay free.
        threading.Thread(target=self.cancel_remote, daemon=True).start()

    def cancel_remote(self) -> None:
        """Ask the server to stop what the source's own connection is running, for a scanner
        that DuckDB's `interrupt()` can't reach. Local sources have nothing to ask."""

    def cancelled(self, op_id: int) -> bool:
        """`op_id` was cancelled: whatever error it ended with, it ended because of that."""
        with self._lock:
            return self._cancelled == op_id

    def check(self) -> None:
        with self._lock:
            if self._cancelled is not None and self._cancelled == self._current:
                raise duckdb.InterruptException("cancelled between statements")

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        """Run the session's own SQL, after checking for a cancel."""
        self.check()
        return self.con.execute(sql, params).fetchall()

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
        """The catalog in the shape upstream's `catalog/extract.py` reads from a connector
        (`connectors/base.py::SQLDatabase`): `database_name`, and lists of records for
        `tables`, `columns`, `views`, `pks`, `fks` and `skipped`."""
        raise NotImplementedError

    def run(self, op: str, args: tuple[Any, ...]) -> Any:
        if op == "rows":
            return self.rows(*args)
        if op == "extract":
            return self.extract()
        raise ValueError(f"unknown operation {op!r}")

    # Building the catalog

    def describe(
        self, relations: Iterable[Relation]
    ) -> tuple[list[Record], list[Record], list[Record]]:
        """`tables`, `columns` and `skipped` for `relations`, with DuckDB's column types, since
        DuckDB is what runs the queries. A relation DuckDB can't describe can't be queried
        either, so it goes in `skipped` and nowhere else: upstream's rule is that every listed
        relation has columns."""
        tables: list[Record] = []
        columns: list[Record] = []
        skipped: list[Record] = []
        for rel in relations:
            try:
                described = self.query(f"DESCRIBE {rel.qualified}")
            except duckdb.InterruptException:
                raise
            except duckdb.Error as e:
                described, error = [], str(e)
            else:
                error = "DuckDB lists no columns for it"
            if not described:
                skipped.append({"table_schema": rel.schema, "table_name": rel.name, "error": error})
                continue
            tables.append(
                {"table_schema": rel.schema, "table_name": rel.name, "table_type": rel.type}
            )
            columns += [
                {
                    "table_schema": rel.schema,
                    "table_name": rel.name,
                    "column_name": column,
                    "data_type": data_type,
                    "is_nullable": null == "YES",
                    "ordinal_position": position,
                }
                for position, (column, data_type, null, *_) in enumerate(described, 1)
            ]
        return tables, columns, skipped

    def catalog(
        self,
        relations: Iterable[Relation],
        *,
        views: Iterable[Record] = (),
        pks: Iterable[Record] = (),
        fks: Iterable[Record] = (),
    ) -> dict[str, Any]:
        """The extract payload: views and keys are kept only for the relations described."""
        tables, columns, skipped = self.describe(relations)
        listed = {(t["table_schema"], t["table_name"]) for t in tables}

        def kept(records: Iterable[Record]) -> list[Record]:
            return [r for r in records if (r["table_schema"], r["table_name"]) in listed]

        return {
            "database_name": self.database_name,
            "tables": tables,
            "columns": columns,
            "views": kept(views),
            "pks": kept(pks),
            "fks": kept(fks),
            "skipped": skipped,
        }


def pk_records(rows: Iterable[tuple[str, str, Sequence[str]]]) -> list[Record]:
    """Primary-key records from `(schema, table, key columns in key order)`."""
    return [
        {"table_schema": schema, "table_name": table, "column_name": column,
         "ordinal_position": position}
        for schema, table, key in rows
        for position, column in enumerate(key, 1)
    ]  # fmt: skip


def fk_records(
    rows: Iterable[tuple[str, str, Sequence[str], str, str, Sequence[str | None]]],
) -> list[Record]:
    """Foreign-key records from `(schema, table, columns, referenced schema, referenced table,
    referenced columns)`, one record per column pair, in key order."""
    return [
        {"table_schema": schema, "table_name": table, "column_name": column,
         "referenced_schema": ref_schema, "referenced_table": ref_table,
         "referenced_column": ref_column}
        for schema, table, cols, ref_schema, ref_table, ref_cols in rows
        for column, ref_column in zip(cols, ref_cols, strict=True)
    ]  # fmt: skip
