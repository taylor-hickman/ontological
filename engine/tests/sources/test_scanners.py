"""Source executors for Postgres and MySQL through DuckDB's scanners (ARCHITECTURE.md §5.5),
with their passwords in the keychain (§4.3).

Postgres runs on the bundled binaries and MySQL on the server ONTOLOGICAL_MYSQL_URL names
(conftest.py). Each source logs in as a reader role that needs its password."""

from __future__ import annotations

import os
import pickle
import signal
import subprocess
import time
from collections.abc import Iterator

import duckdb
import pytest
from conftest import MySQLServer, PgCluster
from fakes import MemoryKeyring
from test_executor import run_in_thread, wait_running

from ontological.sources.executor import (
    Cancelled,
    MySQLSource,
    PostgresSource,
    SourceError,
    SourceExecutor,
    SourceQueryError,
    StatementTimeout,
)
from ontological.sources.keychain import KeychainRef, save_password

PASSWORD = "it's a s3cret; don't log it"

PG_SCHEMA = """
CREATE SCHEMA sales;
CREATE TABLE sales.customer (id int PRIMARY KEY, email text UNIQUE, name text NOT NULL);
CREATE TABLE sales.region (country text, code text, PRIMARY KEY (country, code));
CREATE TABLE sales.orders (
    id int,
    line int,
    customer_id int REFERENCES sales.customer,
    country text,
    code text,
    amount numeric(10, 2),
    placed_at timestamptz,
    PRIMARY KEY (id, line),
    FOREIGN KEY (country, code) REFERENCES sales.region (country, code)
);
CREATE TABLE public.audit (
    order_id int,
    order_line int,
    FOREIGN KEY (order_id, order_line) REFERENCES sales.orders (id, line)
);
CREATE VIEW sales.big_orders AS SELECT id FROM sales.orders WHERE amount > 100;
CREATE MATERIALIZED VIEW sales.order_totals AS
    SELECT customer_id, sum(amount) AS total FROM sales.orders GROUP BY 1;
CREATE TABLE public.events (id int, at date) PARTITION BY RANGE (at);
CREATE TABLE public.events_2026 PARTITION OF public.events
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE SCHEMA private;
CREATE TABLE private.salaries (id int);
CREATE TABLE public.hidden (id int);
INSERT INTO sales.customer VALUES (1, 'a@example.com', 'Ada'), (2, NULL, 'Bo');
INSERT INTO sales.region VALUES ('NZ', 'AKL');
INSERT INTO sales.orders VALUES
    (1, 1, 1, 'NZ', 'AKL', 120.5, now()), (1, 2, 2, 'NZ', 'AKL', 3, now());
INSERT INTO public.events VALUES (1, '2026-03-01');
GRANT USAGE ON SCHEMA sales TO reader;
GRANT SELECT ON ALL TABLES IN SCHEMA sales TO reader;
GRANT SELECT ON public.audit, public.events, public.events_2026 TO reader;
"""


@pytest.fixture(scope="module")
def pg(pg_cluster: PgCluster) -> PgCluster:
    pg_cluster.psql("DROP DATABASE IF EXISTS shop")
    pg_cluster.psql("DROP ROLE IF EXISTS reader")
    pg_cluster.psql("CREATE DATABASE shop")
    pg_cluster.psql(f"CREATE ROLE reader LOGIN PASSWORD '{PASSWORD.replace("'", "''")}'")
    pg_cluster.psql(PG_SCHEMA, db="shop")
    return pg_cluster


@pytest.fixture
def pg_source(pg: PgCluster, keychain: MemoryKeyring) -> PostgresSource:
    ref = keychain_ref("postgres-reader")
    return PostgresSource(str(pg.socket_dir), "shop", "reader", password=ref)


def keychain_ref(account: str) -> KeychainRef:
    return save_password(account, PASSWORD)


@pytest.fixture
def pg_ex(pg_source: PostgresSource) -> Iterator[SourceExecutor]:
    with SourceExecutor(pg_source) as ex:
        yield ex


# Postgres


def test_postgres_extract_lists_relations_as_upstream_does(pg_ex: SourceExecutor) -> None:
    """Views and materialized views keep their type; a partitioned table is listed and its
    partitions aren't; a table the reader can't SELECT from is left out."""
    cat = pg_ex.extract()
    assert cat.database_name == "shop"
    assert [(t["table_schema"], t["table_name"], t["table_type"]) for t in cat.tables] == [
        ("public", "audit", "base table"),
        ("public", "events", "base table"),
        ("sales", "big_orders", "view"),
        ("sales", "customer", "base table"),
        ("sales", "order_totals", "materialized view"),
        ("sales", "orders", "base table"),
        ("sales", "region", "base table"),
    ]
    assert cat.skipped == []
    assert {(c["table_schema"], c["table_name"]) for c in cat.columns} == {
        (t["table_schema"], t["table_name"]) for t in cat.tables
    }
    orders = [
        (c["column_name"], c["data_type"], c["is_nullable"])
        for c in cat.columns
        if c["table_name"] == "orders"
    ]
    assert orders == [
        ("id", "INTEGER", False),
        ("line", "INTEGER", False),
        ("customer_id", "INTEGER", True),
        ("country", "VARCHAR", True),
        ("code", "VARCHAR", True),
        ("amount", "DECIMAL(10,2)", True),
        ("placed_at", "TIMESTAMP WITH TIME ZONE", True),
    ]
    [view] = cat.views
    assert (view["table_schema"], view["table_name"]) == ("sales", "big_orders")
    assert "FROM sales.orders" in view["view_definition"]


def test_postgres_extract_reads_keys_from_pg_constraint(pg_ex: SourceExecutor) -> None:
    cat = pg_ex.extract()
    assert [(k["table_name"], k["column_name"], k["ordinal_position"]) for k in cat.pks] == [
        ("customer", "id", 1),
        ("orders", "id", 1),
        ("orders", "line", 2),
        ("region", "country", 1),
        ("region", "code", 2),
    ]
    assert [
        (f["table_schema"], f["table_name"], f["column_name"],
         f["referenced_schema"], f["referenced_table"], f["referenced_column"])
        for f in cat.fks
    ] == [
        ("public", "audit", "order_id", "sales", "orders", "id"),
        ("public", "audit", "order_line", "sales", "orders", "line"),
        ("sales", "orders", "country", "sales", "region", "country"),
        ("sales", "orders", "code", "sales", "region", "code"),
        ("sales", "orders", "customer_id", "sales", "customer", "id"),
    ]  # fmt: skip


def test_postgres_extract_sees_tables_added_since_the_last(
    pg: PgCluster, pg_ex: SourceExecutor
) -> None:
    pg_ex.extract()
    pg.psql("CREATE TABLE sales.late (id int); GRANT SELECT ON sales.late TO reader", db="shop")
    try:
        assert "late" in {t["table_name"] for t in pg_ex.extract().tables}
    finally:
        pg.psql("DROP TABLE sales.late", db="shop")


def test_postgres_run_sql_runs_duckdb_sql_on_the_remote_tables(pg_ex: SourceExecutor) -> None:
    rows = pg_ex.run_sql(
        "SELECT c.name, sum(o.amount) FROM sales.orders o JOIN sales.customer c "
        "ON c.id = o.customer_id GROUP BY ALL ORDER BY 1"
    )
    assert rows.rows[0][0] == "Ada"
    assert rows.types == ["VARCHAR", "DECIMAL(38,2)"]
    assert pg_ex.run_sql("SELECT count(*) FROM events").rows == [(1,)]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO sales.region VALUES ('AU', 'SYD')",
        "SELECT * FROM postgres_execute('src', 'DELETE FROM sales.region')",
        "SELECT * FROM postgres_query('src', 'DELETE FROM sales.region RETURNING *')",
        "SELECT * FROM postgres_query('src', 'CREATE TABLE public.evil (x int)')",
        "SELECT * FROM private.salaries",
        "SELECT * FROM read_csv('/etc/hosts')",
    ],
)
def test_postgres_is_read_only(pg: PgCluster, pg_ex: SourceExecutor, sql: str) -> None:
    with pytest.raises(SourceQueryError):
        pg_ex.run_sql(sql)
    assert pg.psql("SELECT count(*) FROM sales.region", db="shop") == "1"
    assert pg.psql("SELECT to_regclass('public.evil') IS NULL", db="shop") == "t"


def remote_sleeps(pg: PgCluster) -> int:
    return int(pg.psql(
        "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' "
        "AND query LIKE '%pg_sleep(60)%' AND pid <> pg_backend_pid()"
    ))  # fmt: skip


def wait_no_remote_sleeps(pg: PgCluster) -> None:
    deadline = time.monotonic() + 5
    while remote_sleeps(pg):
        assert time.monotonic() < deadline, "the server is still running the query"
        time.sleep(0.1)


SLEEP = "SELECT * FROM postgres_query('src', 'SELECT pg_sleep(60)')"


def test_a_timeout_cancels_the_query_on_the_server(pg: PgCluster, pg_ex: SourceExecutor) -> None:
    """DuckDB's `interrupt()` can't reach it, so the session asks the server, and the executor
    keeps its child."""
    pid = pg_ex.pid
    started = time.monotonic()
    with pytest.raises(StatementTimeout):
        pg_ex.probe(SLEEP, timeout_s=0.3)
    assert time.monotonic() - started < 1.5
    assert pg_ex.pid == pid
    wait_no_remote_sleeps(pg)
    assert pg_ex.run_sql("SELECT count(*) FROM sales.customer").rows == [(2,)]


def test_cancel_stops_a_remote_scan(pg: PgCluster, pg_ex: SourceExecutor) -> None:
    pg.psql("CREATE OR REPLACE VIEW sales.endless AS SELECT g FROM generate_series(1, 1e12) g; "
            "GRANT SELECT ON sales.endless TO reader", db="shop")  # fmt: skip
    pid = pg_ex.pid
    t, out = run_in_thread(lambda: pg_ex.run_sql("SELECT count(*) FROM sales.endless"))
    wait_running(pg_ex)
    started = time.monotonic()
    pg_ex.cancel()
    t.join(5)
    assert isinstance(out.get("error"), Cancelled)
    assert time.monotonic() - started < 1.5
    assert pg_ex.pid == pid


def test_a_killed_child_s_query_stops_with_it(pg: PgCluster, pg_ex: SourceExecutor) -> None:
    """A child too stuck to cancel is killed after 2 s, and the server, which checks for its
    client every second, stops the query."""
    pid = pg_ex.pid
    assert pid is not None
    t, out = run_in_thread(lambda: pg_ex.probe(SLEEP, timeout_s=0.5))
    wait_running(pg_ex)
    os.kill(pid, signal.SIGSTOP)
    t.join(10)
    assert isinstance(out.get("error"), StatementTimeout)
    assert pg_ex.pid != pid
    wait_no_remote_sleeps(pg)


# Credentials


def test_the_password_is_read_from_the_keychain_by_reference(
    pg: PgCluster, keychain: MemoryKeyring
) -> None:
    ref = keychain_ref("pg")
    source = PostgresSource(str(pg.socket_dir), "shop", "reader", password=ref)
    assert keychain.items == {("Ontological sources", "pg"): PASSWORD}
    assert PASSWORD not in repr(source)
    assert PASSWORD.encode() not in pickle.dumps(source)
    with SourceExecutor(source) as ex:
        assert ex.run_sql("SELECT 1").rows == [(1,)]


def test_a_missing_keychain_entry_fails_and_a_saved_one_is_picked_up(
    pg: PgCluster, keychain: MemoryKeyring
) -> None:
    source = PostgresSource(str(pg.socket_dir), "shop", "reader", password=KeychainRef("later"))
    with SourceExecutor(source) as ex:
        with pytest.raises(SourceError, match="keychain has no password for 'later'"):
            ex.run_sql("SELECT 1")
        keychain_ref("later")
        assert ex.run_sql("SELECT 1").rows == [(1,)]


def test_a_wrong_password_fails(pg: PgCluster, keychain: MemoryKeyring) -> None:
    ref = save_password("wrong", "not it")
    source = PostgresSource(str(pg.socket_dir), "shop", "reader", password=ref)
    with SourceExecutor(source) as ex, pytest.raises(SourceError, match="password"):
        ex.extract()


def test_the_password_stays_in_memory_and_out_of_sight(pg_ex: SourceExecutor) -> None:
    """DuckDB holds it as a temporary secret, which it shows redacted and refuses to show
    otherwise; the child's command line doesn't carry it."""
    [(persistent, storage, shown)] = pg_ex.run_sql(
        "SELECT persistent, storage, secret_string FROM duckdb_secrets()"
    ).rows
    assert (persistent, storage) == (False, "memory")
    assert PASSWORD not in shown
    with pytest.raises(SourceQueryError, match="disabled"):
        pg_ex.run_sql("SELECT secret_string FROM duckdb_secrets(redact = false)")
    args = subprocess.run(
        ["ps", "-o", "args=", "-p", str(pg_ex.pid)], capture_output=True, text=True
    ).stdout
    assert "spawn" in args and PASSWORD not in args


# MySQL

MYSQL_SCHEMA = [
    "DROP DATABASE IF EXISTS src_shop",
    "DROP DATABASE IF EXISTS src_other",
    "CREATE DATABASE src_shop",
    "CREATE DATABASE src_other",
    "CREATE TABLE src_other.elsewhere (id int)",
    "CREATE TABLE src_shop.customer (id int PRIMARY KEY, email varchar(80) UNIQUE, "
    "name varchar(50) NOT NULL)",
    "CREATE TABLE src_shop.region (country varchar(2), code varchar(3), "
    "PRIMARY KEY (country, code))",
    "CREATE TABLE src_shop.orders (id int, line int, customer_id int, country varchar(2), "
    "code varchar(3), amount decimal(10, 2), placed_at datetime, PRIMARY KEY (id, line), "
    "FOREIGN KEY (customer_id) REFERENCES src_shop.customer (id), "
    "FOREIGN KEY (country, code) REFERENCES src_shop.region (country, code))",
    "CREATE VIEW src_shop.big_orders AS SELECT id FROM src_shop.orders WHERE amount > 100",
    "INSERT INTO src_shop.customer VALUES (1, 'a@example.com', 'Ada'), (2, NULL, 'Bo')",
    "INSERT INTO src_shop.region VALUES ('NZ', 'AKL')",
    "INSERT INTO src_shop.orders VALUES (1, 1, 1, 'NZ', 'AKL', 120.5, now()), "
    "(1, 2, 2, 'NZ', 'AKL', 3, now())",
    "DROP USER IF EXISTS 'src_reader'@'%'",
    "CREATE USER 'src_reader'@'%' IDENTIFIED BY '{password}'",
    # Without SHOW VIEW, MySQL shows a view's definition as ''.
    "GRANT SELECT, SHOW VIEW ON src_shop.* TO 'src_reader'@'%'",
]


def mysql_admin(server: MySQLServer, statements: list[str]) -> None:
    con = duckdb.connect()
    con.execute(f"ATTACH '{server.dsn}' AS admin (TYPE mysql)")
    for statement in statements:
        con.execute("CALL mysql_execute('admin', ?)", [statement])
    con.close()


@pytest.fixture(scope="module")
def mysql(mysql_server: MySQLServer) -> Iterator[MySQLServer]:
    password = PASSWORD.replace("'", "''")
    mysql_admin(mysql_server, [s.replace("{password}", password) for s in MYSQL_SCHEMA])
    yield mysql_server
    mysql_admin(
        mysql_server,
        ["DROP DATABASE src_shop", "DROP DATABASE src_other", "DROP USER 'src_reader'@'%'"],
    )


@pytest.fixture
def mysql_ex(mysql: MySQLServer, keychain: MemoryKeyring) -> Iterator[SourceExecutor]:
    ref = keychain_ref("mysql-reader")
    source = MySQLSource(mysql.host, "src_shop", "src_reader", port=mysql.port, password=ref)
    with SourceExecutor(source) as ex:
        yield ex


def test_mysql_extract_reads_its_own_database(mysql_ex: SourceExecutor) -> None:
    cat = mysql_ex.extract()
    assert cat.database_name == "src_shop"
    assert cat.tables == [
        {"table_schema": "src_shop", "table_name": "big_orders", "table_type": "view"},
        {"table_schema": "src_shop", "table_name": "customer", "table_type": "base table"},
        {"table_schema": "src_shop", "table_name": "orders", "table_type": "base table"},
        {"table_schema": "src_shop", "table_name": "region", "table_type": "base table"},
    ]
    assert cat.skipped == []
    orders = [
        (c["column_name"], c["data_type"], c["is_nullable"])
        for c in cat.columns
        if c["table_name"] == "orders"
    ]
    assert orders == [
        ("id", "INTEGER", False),
        ("line", "INTEGER", False),
        ("customer_id", "INTEGER", True),
        ("country", "VARCHAR", True),
        ("code", "VARCHAR", True),
        ("amount", "DECIMAL(10,2)", True),
        ("placed_at", "TIMESTAMP", True),
    ]
    [view] = cat.views
    assert view["table_name"] == "big_orders"
    assert "`orders`" in view["view_definition"]


def test_mysql_extract_reads_keys(mysql_ex: SourceExecutor) -> None:
    cat = mysql_ex.extract()
    assert [(k["table_name"], k["column_name"], k["ordinal_position"]) for k in cat.pks] == [
        ("customer", "id", 1),
        ("orders", "id", 1),
        ("orders", "line", 2),
        ("region", "country", 1),
        ("region", "code", 2),
    ]
    assert {
        (f["table_name"], f["column_name"], f["referenced_schema"], f["referenced_table"],
         f["referenced_column"])
        for f in cat.fks
    } == {
        ("orders", "customer_id", "src_shop", "customer", "id"),
        ("orders", "country", "src_shop", "region", "country"),
        ("orders", "code", "src_shop", "region", "code"),
    }  # fmt: skip
    pairs = [(f["column_name"], f["referenced_column"]) for f in cat.fks]
    assert pairs.index(("country", "country")) + 1 == pairs.index(("code", "code"))


def test_mysql_run_sql_and_read_only(mysql: MySQLServer, mysql_ex: SourceExecutor) -> None:
    assert mysql_ex.run_sql("SELECT name FROM customer ORDER BY id").rows == [("Ada",), ("Bo",)]
    for sql in (
        "INSERT INTO region VALUES ('AU', 'SYD')",
        "SELECT * FROM mysql_execute('src', 'DELETE FROM src_shop.region')",
        "SELECT * FROM mysql_query('src', 'DELETE FROM src_shop.region')",
        "SELECT * FROM src_other.elsewhere",
    ):
        with pytest.raises(SourceQueryError):
            mysql_ex.run_sql(sql)
    assert mysql_ex.run_sql("SELECT count(*) FROM region").rows == [(1,)]


def test_mysql_wrong_password_fails(mysql: MySQLServer, keychain: MemoryKeyring) -> None:
    ref = save_password("mysql-wrong", "not it")
    source = MySQLSource(mysql.host, "src_shop", "src_reader", port=mysql.port, password=ref)
    with SourceExecutor(source) as ex, pytest.raises(SourceError, match="Access denied"):
        ex.extract()
