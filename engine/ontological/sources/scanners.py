"""Postgres and MySQL databases read through DuckDB's `postgres_scanner` and `mysql_scanner`
(ARCHITECTURE.md §5.5).

The password reaches DuckDB as a temporary secret, which lives in memory only and which
`duckdb_secrets()` shows redacted. The database is attached `READ_ONLY`, so the scanner runs
every remote statement in a read-only transaction: `postgres_execute()`, `mysql_execute()` and
a write inside `postgres_query()` are refused by the server.

DuckDB's `interrupt()` doesn't reach a query the server is running, so a Postgres session also
asks the server to cancel it, and tells the server to check for a vanished client: when the
engine kills a child, its query stops with it. MySQL stops a dead client's query by itself.

DuckDB's view of a remote catalog types every relation as a base table and lists no foreign
keys, so relations, view definitions and keys come from the server's own catalog through
`postgres_query()` and `mysql_query()`, as upstream's connectors read them. Column types are
DuckDB's, since DuckDB runs the queries.

A Postgres source's query history is upstream's: `pg_stat_activity`, read through
`postgres_query()`. Upstream had no MySQL connector, and a MySQL session reads no history.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

import duckdb

from ontological.sources.session import (
    ALIAS,
    BASE_TABLE,
    MATERIALIZED_VIEW,
    VIEW,
    Record,
    Relation,
    Session,
    quote_literal,
)
from ontological.sources.spec import MySQLSource, PostgresSource

_SECRET = "src_secret"


class _ScannerSession(Session):
    kind: str  # DuckDB's attach type, and the prefix of its functions

    def attach(self, options: str) -> None:
        """`options` are connection options beside the secret's, in the scanner's own syntax."""
        self.con.execute(
            f"ATTACH {quote_literal(options)} AS {ALIAS} "
            f"(TYPE {self.kind}, SECRET {_SECRET}, READ_ONLY)"
        )

    def __init__(self, source: PostgresSource | MySQLSource, password: str | None) -> None:
        self.database_name = source.database_name
        super().__init__()
        options = [
            f"HOST {quote_literal(source.host)}",
            f"PORT {int(source.port)}",
            f"DATABASE {quote_literal(source.database)}",
            f"USER {quote_literal(source.user)}",
        ]
        if password is not None:
            options.append(f"PASSWORD {quote_literal(password)}")
        self.con.execute(
            f"CREATE TEMPORARY SECRET {_SECRET} (TYPE {self.kind}, {', '.join(options)})"
        )
        self.attach("")
        self.con.execute(f"USE {ALIAS}")
        self.lock_down()

    def remote(self, sql: str) -> list[tuple[Any, ...]]:
        """`sql` run by the server itself, on the attached connection."""
        return self.query(f"SELECT * FROM {self.kind}_query('{ALIAS}', {quote_literal(sql)})")

    def clear_cache(self) -> None:
        """The scanner caches the remote catalog. Extract has to see the tables as they are
        now."""
        self.query(f"CALL {self.kind}_clear_cache()")


#: Every executor's Postgres connections name themselves with it.
_APPLICATION_PREFIX = "ontological-source-"
_PG_SKIP_SCHEMAS = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp\\_%'"
)
_PG_RELKINDS = {"r": BASE_TABLE, "p": BASE_TABLE, "f": BASE_TABLE, "v": VIEW,
                "m": MATERIALIZED_VIEW}  # fmt: skip


class PostgresSession(_ScannerSession):
    kind = "postgres"
    extension = "postgres_scanner"

    def attach(self, options: str) -> None:
        """Every connection names this session in `application_name`, which is how
        `cancel_remote()` finds it, and has the server check every second that the child is
        still there (Postgres 14 and later; an older server is attached without it)."""
        self.application_name = f"{_APPLICATION_PREFIX}{secrets.token_hex(6)}"
        named = f"{options} application_name={self.application_name}".strip()
        try:
            super().attach(f"{named} options='-c client_connection_check_interval=1000'")
        except duckdb.Error as e:
            if "client_connection_check_interval" not in str(e):
                raise
            super().attach(named)

    def cancel_remote(self) -> None:
        """`pg_cancel_backend()`, from a connection of its own, on whatever this session's other
        connections are running. A role may cancel its own backends."""
        try:
            cursor = self.con.cursor()
            try:
                cursor.execute(
                    f"SELECT * FROM postgres_query('{ALIAS}', "
                    + quote_literal(
                        "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                        f"WHERE application_name = {quote_literal(self.application_name)} "
                        "AND state = 'active' AND pid <> pg_backend_pid()"
                    )
                    + ")"
                ).fetchall()
            finally:
                cursor.close()
        except duckdb.Error:
            pass  # the engine kills the child if the query doesn't stop

    def extract(self) -> dict[str, Any]:
        """Relations as upstream's Postgres connector lists them, from `pg_class`: tables,
        partitioned tables (not their partitions), foreign tables, views and materialized
        views, which `information_schema.tables` leaves out. Only those the user can SELECT
        from are listed."""
        self.clear_cache()
        relations = [
            Relation(schema, name, _PG_RELKINDS[kind])
            for schema, name, kind in self.remote(
                "SELECT n.nspname::text, c.relname::text, c.relkind::text "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm') AND NOT c.relispartition "
                f"AND {_PG_SKIP_SCHEMAS} AND has_table_privilege(c.oid, 'SELECT') "
                "ORDER BY 1, 2"
            )
        ]
        views = [
            {"table_schema": schema, "table_name": name, "view_definition": definition}
            for schema, name, definition in self.remote(
                "SELECT n.nspname::text, c.relname::text, pg_get_viewdef(c.oid) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE c.relkind = 'v' AND {_PG_SKIP_SCHEMAS} ORDER BY 1, 2"
            )
        ]
        keys = (
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL unnest(con.conkey, con.confkey) "
            "WITH ORDINALITY AS k(attnum, refnum, ord) "
            "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum "
        )
        pks = [
            {"table_schema": schema, "table_name": table, "column_name": column,
             "ordinal_position": position}
            for schema, table, column, position in self.remote(
                "SELECT n.nspname::text, c.relname::text, a.attname::text, k.ord::int "
                f"{keys} WHERE con.contype = 'p' ORDER BY 1, 2, 4"
            )
        ]  # fmt: skip
        fks = [
            {"table_schema": schema, "table_name": table, "column_name": column,
             "referenced_schema": ref_schema, "referenced_table": ref_table,
             "referenced_column": ref_column}
            for schema, table, column, ref_schema, ref_table, ref_column in self.remote(
                "SELECT n.nspname::text, c.relname::text, a.attname::text, "
                "rn.nspname::text, rc.relname::text, ra.attname::text "
                f"{keys}"
                "JOIN pg_class rc ON rc.oid = con.confrelid "
                "JOIN pg_namespace rn ON rn.oid = rc.relnamespace "
                "JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = k.refnum "
                "WHERE con.contype = 'f' ORDER BY 1, 2, con.conname, k.ord"
            )
        ]  # fmt: skip
        return self.catalog(relations, views=views, pks=pks, fks=fks)

    def history(self, hours: int) -> dict[str, Any]:
        """Upstream's `get_queries`: each backend's latest statement, from `pg_stat_activity`,
        if it ended (`state_change`) in the last `hours` hours. Only this database's client
        backends are read, so a parallel worker doesn't count its leader's statement again, and
        no executor's own connections, whose statements are DuckDB's scans. A role sees the text
        of its own backends' statements only, unless it holds `pg_read_all_stats`; the others
        read `<insufficient privilege>` and are left out."""
        rows = self.remote(
            "SELECT query, extract(epoch FROM state_change)::float8 FROM pg_stat_activity "
            "WHERE datname = current_database() AND backend_type = 'client backend' "
            "AND pid <> pg_backend_pid() "
            f"AND application_name NOT LIKE {quote_literal(_APPLICATION_PREFIX + '%')} "
            "AND query IS NOT NULL AND query <> '' AND query <> '<insufficient privilege>' "
            f"AND state_change >= now() - make_interval(hours => {int(hours)}) "
            "ORDER BY state_change DESC"
        )
        return {
            "dialects": ["postgres"],
            "rows": [
                {"query_text": query, "end_time": datetime.fromtimestamp(ended, UTC), "count": 1}
                for query, ended in rows
            ],
        }

    def json_columns(self, schema: str, table: str) -> set[str]:
        """`json` and `jsonb` columns, which the scanner reads as VARCHAR."""
        rows = self.remote(
            "SELECT a.attname::text FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = {quote_literal(schema)} AND c.relname = {quote_literal(table)} "
            "AND a.attnum > 0 AND NOT a.attisdropped AND a.atttypid IN ('json'::regtype, "
            "'jsonb'::regtype)"
        )
        return {name for (name,) in rows}


class MySQLSession(_ScannerSession):
    """MySQL's schemas are its databases. The source is one of them, the one its spec names;
    DuckDB shows it as the schema of that name."""

    kind = "mysql"
    extension = "mysql_scanner"

    def remote(self, sql: str) -> list[tuple[Any, ...]]:
        """The scanner hands MySQL's `information_schema` names over as BLOBs."""
        return [
            tuple(v.decode() if isinstance(v, bytes) else v for v in row)
            for row in super().remote(sql)
        ]

    def json_columns(self, schema: str, table: str) -> set[str]:
        """`JSON` columns, which the scanner reads as VARCHAR."""
        rows = self.remote(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = {quote_literal(schema)} AND TABLE_NAME = {quote_literal(table)} "
            "AND DATA_TYPE = 'json'"
        )
        return {name for (name,) in rows}

    def extract(self) -> dict[str, Any]:
        self.clear_cache()
        relations = [
            Relation(schema, name, VIEW if kind == "VIEW" else BASE_TABLE)
            for schema, name, kind in self.remote(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                "ORDER BY TABLE_NAME"
            )
        ]
        views = [
            {"table_schema": schema, "table_name": name, "view_definition": definition}
            for schema, name, definition in self.remote(
                "SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS "
                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
            )
        ]
        usage = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, "
            "REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = DATABASE() "
        )
        pks = [
            {"table_schema": schema, "table_name": table, "column_name": column,
             "ordinal_position": int(position)}
            for schema, table, column, position, *_ in self.remote(
                f"{usage} AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY TABLE_NAME, ORDINAL_POSITION"
            )
        ]  # fmt: skip
        fks: list[Record] = [
            {"table_schema": schema, "table_name": table, "column_name": column,
             "referenced_schema": ref_schema, "referenced_table": ref_table,
             "referenced_column": ref_column}
            for schema, table, column, _, ref_schema, ref_table, ref_column in self.remote(
                f"{usage} AND REFERENCED_TABLE_NAME IS NOT NULL "
                "ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION"
            )
        ]  # fmt: skip
        return self.catalog(relations, views=views, pks=pks, fks=fks)
