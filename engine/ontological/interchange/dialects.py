"""The SQL dialect of each catalog database, from the source its catalog row keeps (M2.6).

Upstream asked every live connector for its dialect. Here the source spec says what kind of source
a database is, which names its sqlglot dialect, with no connection made. File sources (CSV,
Parquet, DuckDB) are read through DuckDB, so their SQL is DuckDB's.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

from sqlalchemy import select

from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store

#: sqlglot's generic parser, then Postgres: upstream's dialects for a database with no connector to
#: name one (`server/sql_utils.py`), and compile's (ontological/compile/sql_attribute_writes.py).
FALLBACK = ("", "postgres")

_BY_KIND = {
    "postgres": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
    "duckdb": "duckdb",
    "files": "duckdb",
}


def dialect_by_database_name() -> dict[str, str]:
    """Each catalog database's dialect, by name: the row's own `dialect` when something set it,
    else its source's. A database with neither has none."""
    db = s.catalog_database
    rows = store().query_read(select(db.c.name, db.c.dialect, db.c.connection))
    found: dict[str, str] = {}
    for row in rows:
        connection = row["connection"]
        kind = connection.get("kind") if isinstance(connection, dict) else None
        dialect = row["dialect"] or (_BY_KIND.get(kind) if isinstance(kind, str) else None)
        if dialect:
            found[str(row["name"])] = str(dialect)
    return found


def parse_dialects(database_name: str | None) -> list[str]:
    """The dialects to parse the database's SQL with, in turn: its own, then the fallbacks."""
    own = dialect_by_database_name().get(database_name or "")
    return list(dict.fromkeys([own, *FALLBACK] if own else FALLBACK))
