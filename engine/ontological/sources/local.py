"""DuckDB files, and Parquet, CSV and JSON files, read through DuckDB itself (ARCHITECTURE.md
§5.5)."""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Any

from ontological.sources.session import (
    ALIAS,
    BASE_TABLE,
    MATERIALIZED_VIEW,
    VIEW,
    Relation,
    Session,
    fk_records,
    pk_records,
    quote_ident,
    quote_literal,
)
from ontological.sources.spec import DuckDBSource, FileSource, FileTable

_TABLE_TYPES = {"BASE TABLE": BASE_TABLE, "VIEW": VIEW, "MATERIALIZED VIEW": MATERIALIZED_VIEW}


class DuckDBSession(Session):
    """A DuckDB database file, attached read-only. Its catalog is upstream's DuckDB reference
    connector finished: `table_type` from `information_schema.tables`, and the primary and
    foreign keys `get_pks` and `get_fks` left empty, from `duckdb_constraints()`."""

    def __init__(self, source: DuckDBSource) -> None:
        if not source.path.is_file():
            raise FileNotFoundError(f"DuckDB database not found: {source.path}")
        self.database_name = source.database_name
        super().__init__()
        self.con.execute(f"ATTACH {quote_literal(str(source.path))} AS {ALIAS} (READ_ONLY)")
        self.con.execute(f"USE {ALIAS}")
        self.lock_down()

    def extract(self) -> dict[str, Any]:
        listed: list[tuple[str, str, str]] = self.query(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "WHERE table_catalog = ? ORDER BY table_schema, table_name",
            [ALIAS],
        )
        relations = [
            Relation(schema, name, _TABLE_TYPES.get(kind, BASE_TABLE))
            for schema, name, kind in listed
        ]
        views = [
            {"table_schema": schema, "table_name": name, "view_definition": sql}
            for schema, name, sql in self.query(
                "SELECT schema_name, view_name, sql FROM duckdb_views() "
                "WHERE database_name = ? AND NOT internal ORDER BY schema_name, view_name",
                [ALIAS],
            )
        ]
        constraints = (
            "SELECT schema_name, table_name, constraint_column_names, referenced_table, "
            "referenced_column_names FROM duckdb_constraints() "
            "WHERE database_name = ? AND constraint_type = ? "
            "ORDER BY schema_name, table_name, constraint_index"
        )
        pks = pk_records(
            (schema, table, cols)
            for schema, table, cols, *_ in self.query(constraints, [ALIAS, "PRIMARY KEY"])
        )
        # DuckDB refuses a foreign key across schemas, so the referenced table is in the
        # referencing table's schema.
        fks = fk_records(
            (schema, table, cols, schema, ref_table, ref_cols)
            for schema, table, cols, ref_table, ref_cols in self.query(
                constraints, [ALIAS, "FOREIGN KEY"]
            )
        )
        return self.catalog(relations, views=views, pks=pks, fks=fks)


_READERS = {"parquet": "read_parquet", "csv": "read_csv", "json": "read_json"}


class FileSession(Session):
    """Each file, or glob of files, is a view in the in-memory catalog that reads it with
    `read_parquet`, `read_csv` or `read_json`. Its catalog lists them as base tables: the
    views are how DuckDB reaches the files, not definitions anyone wrote. Only those files can
    be read once the session is locked down."""

    def __init__(self, source: FileSource) -> None:
        self.database_name = source.database_name
        self.tables = source.tables
        super().__init__()
        paths: list[str] = []
        directories: list[str] = []
        for table in source.tables:
            path, folder = _resolve(table)
            if folder is None:
                paths.append(path)
            else:
                directories.append(folder)
            reader = _READERS[table.format]
            self.con.execute(
                f"CREATE VIEW main.{quote_ident(table.name)} AS "
                f"SELECT * FROM {reader}({quote_literal(path)})"
            )
        self.lock_down(paths=paths, directories=directories)

    catalog_name = "memory"  # each file is a view in the session's own database

    def extract(self) -> dict[str, Any]:
        relations = [Relation("main", t.name, BASE_TABLE, self.catalog_name) for t in self.tables]
        return self.catalog(relations)


def _resolve(table: FileTable) -> tuple[str, str | None]:
    """The absolute path for the reader, and for a glob the folder its matches sit under: its
    parts up to the first wildcard. A missing file fails here; a glob that matches nothing
    fails when its view is made."""
    path = Path(table.path).expanduser()
    if not glob.has_magic(table.path):
        if not path.is_file():
            raise FileNotFoundError(f"file not found for table {table.name!r}: {path}")
        return str(path.resolve()), None
    fixed: list[str] = []
    for part in path.parts:
        if glob.has_magic(part):
            break
        fixed.append(part)
    prefix = Path(*fixed)
    folder = prefix.resolve()
    return str(folder / path.relative_to(prefix)), os.path.join(folder, "")
