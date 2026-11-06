"""A SQLite file attached through DuckDB's SQLite scanner (ARCHITECTURE.md §5.5)."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Generator
from typing import Any
from urllib.parse import quote

from ontological.sources.session import (
    ALIAS,
    BASE_TABLE,
    VIEW,
    Record,
    Relation,
    Session,
    fk_records,
    pk_records,
    quote_literal,
)
from ontological.sources.spec import SqliteSource


class SqliteSession(Session):
    def __init__(self, source: SqliteSource) -> None:
        path = source.path
        if not path.is_file():
            raise FileNotFoundError(f"SQLite database not found: {path}")
        self.path = path
        self.database_name = source.database_name
        with self._schema() as schema:  # fails here if the file isn't a SQLite database
            schema.execute("SELECT count(*) FROM sqlite_master").fetchall()
        super().__init__()
        self.con.execute(f"ATTACH {quote_literal(str(path))} AS {ALIAS} (TYPE sqlite, READ_ONLY)")
        self.con.execute(f"USE {ALIAS}")
        self.lock_down()

    def extract(self) -> dict[str, Any]:
        """Relations, view definitions and keys come from SQLite's own schema, as upstream's
        SQLite connector reads them: DuckDB's scanner lists no foreign keys, and one view it
        can't translate makes `information_schema.tables` fail for the whole file."""
        with self._schema() as schema:
            listed: list[tuple[str, str, str]] = schema.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            relations = [
                Relation("main", name, VIEW if kind == "view" else BASE_TABLE)
                for name, kind, _ in listed
            ]
            views = [
                {"table_schema": "main", "table_name": name, "view_definition": sql}
                for name, kind, sql in listed
                if kind == "view"
            ]
            self.check()
            base = [name for name, kind, _ in listed if kind == "table"]
            pks, fks = self._keys(schema, base)
        return self.catalog(relations, views=views, pks=pks, fks=fks)

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
        for table in tables:
            info = con.execute("SELECT name, pk FROM pragma_table_info(?)", (table,))
            pk_columns[table] = [name for _, name in sorted((pk, n) for n, pk in info if pk > 0)]
        fks: list[tuple[str, str, list[str], str, str, list[str | None]]] = []
        for table in tables:
            listed: list[tuple[int, int, str, str, str | None]] = con.execute(
                'SELECT id, seq, "table", "from", "to" FROM pragma_foreign_key_list(?) '
                "ORDER BY id, seq",
                (table,),
            ).fetchall()
            for _, seq, ref_table, from_col, referenced in listed:
                if referenced is None:  # REFERENCES t with no column means t's primary key
                    ref_key = pk_columns.get(ref_table, [])
                    referenced = ref_key[seq] if seq < len(ref_key) else None
                fks.append(("main", table, [from_col], "main", ref_table, [referenced]))
        pks = pk_records(("main", table, key) for table, key in pk_columns.items() if key)
        return pks, fk_records(fks)
