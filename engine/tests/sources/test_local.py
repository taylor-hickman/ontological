"""Source executors for a DuckDB file and for Parquet, CSV and JSON files (ARCHITECTURE.md §5.5).

The DuckDB catalog is upstream's DuckDB reference connector finished: `table_type`, and the
primary and foreign keys its `get_pks` and `get_fks` leave empty."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from ontological.sources.executor import (
    DuckDBSource,
    FileSource,
    FileTable,
    SourceError,
    SourceExecutor,
    SourceQueryError,
)

DUCKDB_SCHEMA = """
CREATE SCHEMA sales;
CREATE TABLE sales.customer (id INTEGER PRIMARY KEY, email VARCHAR UNIQUE, name VARCHAR NOT NULL);
CREATE TABLE sales.region (country VARCHAR, code VARCHAR, PRIMARY KEY (country, code));
CREATE TABLE sales.orders (
    id INTEGER,
    line INTEGER,
    customer_id INTEGER REFERENCES sales.customer (id),
    country VARCHAR,
    code VARCHAR,
    amount DECIMAL(10, 2),
    PRIMARY KEY (id, line),
    FOREIGN KEY (country, code) REFERENCES sales.region (country, code)
);
CREATE VIEW sales.big_orders AS SELECT id FROM sales.orders WHERE amount > 100;
CREATE TABLE main.note (body VARCHAR);
INSERT INTO sales.customer VALUES (1, 'a@example.com', 'Ada'), (2, NULL, 'Bo');
INSERT INTO sales.region VALUES ('NZ', 'AKL');
INSERT INTO sales.orders VALUES (1, 1, 1, 'NZ', 'AKL', 120.5), (1, 2, 2, 'NZ', 'AKL', 3);
"""


@pytest.fixture(scope="module")
def duck(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("duck") / "shop.duckdb"
    con = duckdb.connect(str(path))
    con.execute(DUCKDB_SCHEMA)
    con.close()
    return path


@pytest.fixture
def duck_ex(duck: Path) -> Iterator[SourceExecutor]:
    with SourceExecutor(DuckDBSource(duck)) as ex:
        yield ex


# A DuckDB file


def test_duckdb_extract_lists_every_schema_with_its_table_type(duck_ex: SourceExecutor) -> None:
    cat = duck_ex.extract()
    assert cat.database_name == "shop"
    assert cat.tables == [
        {"table_schema": "main", "table_name": "note", "table_type": "base table"},
        {"table_schema": "sales", "table_name": "big_orders", "table_type": "view"},
        {"table_schema": "sales", "table_name": "customer", "table_type": "base table"},
        {"table_schema": "sales", "table_name": "orders", "table_type": "base table"},
        {"table_schema": "sales", "table_name": "region", "table_type": "base table"},
    ]
    assert cat.skipped == []
    assert {(c["table_schema"], c["table_name"]) for c in cat.columns} == {
        (t["table_schema"], t["table_name"]) for t in cat.tables
    }
    customer = [
        (c["column_name"], c["data_type"], c["is_nullable"], c["ordinal_position"])
        for c in cat.columns
        if c["table_name"] == "customer"
    ]
    assert customer == [
        ("id", "INTEGER", False, 1),
        ("email", "VARCHAR", True, 2),
        ("name", "VARCHAR", False, 3),
    ]
    amount = next(c for c in cat.columns if c["column_name"] == "amount")
    assert amount["data_type"] == "DECIMAL(10,2)"
    assert cat.views == [
        {
            "table_schema": "sales",
            "table_name": "big_orders",
            "view_definition": "CREATE VIEW sales.big_orders AS SELECT id FROM sales.orders "
            "WHERE (amount > 100);",
        }
    ]


def test_duckdb_extract_reads_primary_keys_in_key_order(duck_ex: SourceExecutor) -> None:
    assert duck_ex.extract().pks == [
        {"table_schema": "sales", "table_name": "customer", "column_name": "id",
         "ordinal_position": 1},
        {"table_schema": "sales", "table_name": "orders", "column_name": "id",
         "ordinal_position": 1},
        {"table_schema": "sales", "table_name": "orders", "column_name": "line",
         "ordinal_position": 2},
        {"table_schema": "sales", "table_name": "region", "column_name": "country",
         "ordinal_position": 1},
        {"table_schema": "sales", "table_name": "region", "column_name": "code",
         "ordinal_position": 2},
    ]  # fmt: skip


def test_duckdb_extract_pairs_composite_foreign_key_columns(duck_ex: SourceExecutor) -> None:
    """A UNIQUE constraint is not a key upstream's catalog reads."""
    assert duck_ex.extract().fks == [
        {"table_schema": "sales", "table_name": "orders", "column_name": "customer_id",
         "referenced_schema": "sales", "referenced_table": "customer",
         "referenced_column": "id"},
        {"table_schema": "sales", "table_name": "orders", "column_name": "country",
         "referenced_schema": "sales", "referenced_table": "region",
         "referenced_column": "country"},
        {"table_schema": "sales", "table_name": "orders", "column_name": "code",
         "referenced_schema": "sales", "referenced_table": "region",
         "referenced_column": "code"},
    ]  # fmt: skip


def test_duckdb_run_sql_reads_the_file(duck_ex: SourceExecutor) -> None:
    rows = duck_ex.run_sql("SELECT name FROM sales.customer ORDER BY id")
    assert rows.rows == [("Ada",), ("Bo",)]
    assert duck_ex.run_sql("SELECT count(*) FROM sales.big_orders").rows == [(1,)]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO sales.customer VALUES (3, NULL, 'x')",
        "SELECT * FROM read_csv('/etc/hosts')",
        "ATTACH '/tmp/other.duckdb' AS other",
    ],
)
def test_duckdb_runs_one_select_on_its_own_file(duck_ex: SourceExecutor, sql: str) -> None:
    with pytest.raises(SourceQueryError):
        duck_ex.run_sql(sql)


def test_the_duckdb_file_is_opened_read_only(duck: Path) -> None:
    before = duck.read_bytes()
    with SourceExecutor(DuckDBSource(duck)) as ex:
        ex.extract()
        ex.run_sql("SELECT * FROM sales.orders")
    assert duck.read_bytes() == before
    assert not duck.with_suffix(".duckdb.wal").exists()


def test_a_missing_duckdb_file_fails_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "gone.duckdb"
    with SourceExecutor(DuckDBSource(missing)) as ex, pytest.raises(SourceError, match="gone"):
        ex.extract()
    assert not missing.exists()


# Parquet, CSV and JSON files


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("files") / "warehouse"
    (root / "events").mkdir(parents=True)
    con = duckdb.connect()

    def copy(sql: str, to: Path, options: str = "") -> None:
        con.execute(f"COPY ({sql}) TO '{to}' {options}")

    copy("SELECT range AS id, 'person ' || range AS name FROM range(5)", root / "people.parquet")
    copy("SELECT range AS id, range * 2.5 AS amount, DATE '2026-01-01' + range::INT AS paid_on "
         "FROM range(3)", root / "payments.csv")  # fmt: skip
    copy("SELECT range AS id, {'kind': 'click', 'n': range} AS detail FROM range(4)",
         root / "clicks.json")  # fmt: skip
    copy("SELECT range AS id FROM range(3)", root / "rates.csv.gz", "(COMPRESSION gzip)")
    copy("SELECT range AS id FROM range(3)", root / "events" / "2026-01.parquet")
    copy("SELECT range + 10 AS id FROM range(4)", root / "events" / "2026-02.parquet")
    con.close()
    (root / "secret.csv").write_text("token\nhunter2\n")
    return root


@pytest.fixture
def file_source(files: Path) -> FileSource:
    return FileSource(
        "warehouse",
        (
            FileTable.of(files / "people.parquet"),
            FileTable.of(files / "payments.csv"),
            FileTable.of(files / "clicks.json"),
            FileTable.of(files / "rates.csv.gz"),
            FileTable("events", str(files / "events" / "*.parquet"), "parquet"),
        ),
    )


@pytest.fixture
def files_ex(file_source: FileSource) -> Iterator[SourceExecutor]:
    with SourceExecutor(file_source) as ex:
        yield ex


def test_file_tables_are_named_after_their_files(files: Path) -> None:
    source = FileSource.of(files / "people.parquet", files / "rates.csv.gz", files / "clicks.json")
    assert source.name == "warehouse"
    assert source.tables == (
        FileTable("people", str(files / "people.parquet"), "parquet"),
        FileTable("rates", str(files / "rates.csv.gz"), "csv"),
        FileTable("clicks", str(files / "clicks.json"), "json"),
    )
    with pytest.raises(ValueError, match="format"):
        FileTable.of(files / "notes.txt")
    with pytest.raises(ValueError, match="same table"):
        FileSource.of(files / "people.parquet", files / "People.csv")


def test_files_extract_lists_each_file_as_a_base_table(files_ex: SourceExecutor) -> None:
    cat = files_ex.extract()
    assert cat.database_name == "warehouse"
    assert cat.tables == [
        {"table_schema": "main", "table_name": name, "table_type": "base table"}
        for name in ("people", "payments", "clicks", "rates", "events")
    ]
    assert cat.views == [] and cat.pks == [] and cat.fks == [] and cat.skipped == []
    types = {(c["table_name"], c["column_name"]): c["data_type"] for c in cat.columns}
    assert types == {
        ("people", "id"): "BIGINT",
        ("people", "name"): "VARCHAR",
        ("payments", "id"): "BIGINT",
        ("payments", "amount"): "DOUBLE",
        ("payments", "paid_on"): "DATE",
        ("clicks", "id"): "BIGINT",
        ("clicks", "detail"): "STRUCT(kind VARCHAR, n BIGINT)",
        ("rates", "id"): "BIGINT",
        ("events", "id"): "BIGINT",
    }


def test_files_run_sql_reads_every_file_of_a_glob(files_ex: SourceExecutor) -> None:
    assert files_ex.run_sql("SELECT count(*), max(id) FROM events").rows == [(7, 13)]
    assert files_ex.run_sql("SELECT detail.kind FROM clicks LIMIT 1").rows == [("click",)]
    assert files_ex.run_sql("SELECT sum(id) FROM rates").rows == [(3,)]


def test_files_can_be_read_by_path_but_no_others(files_ex: SourceExecutor, files: Path) -> None:
    own = files_ex.run_sql(f"SELECT count(*) FROM read_parquet('{files / 'people.parquet'}')")
    assert own.rows == [(5,)]
    for sql in (
        f"SELECT * FROM read_csv('{files / 'secret.csv'}')",
        "SELECT * FROM read_csv('/etc/hosts')",
        f"SELECT * FROM glob('{files}/*')",
    ):
        with pytest.raises(SourceQueryError):
            files_ex.run_sql(sql)


def test_a_missing_file_fails(files: Path) -> None:
    source = FileSource.of(files / "people.parquet", files / "gone.csv")
    with SourceExecutor(source) as ex, pytest.raises(SourceError, match="gone"):
        ex.extract()


def test_a_glob_that_matches_nothing_fails(files: Path) -> None:
    source = FileSource("w", (FileTable("none", str(files / "nothing" / "*.parquet"), "parquet"),))
    with SourceExecutor(source) as ex, pytest.raises(SourceError, match="No files found"):
        ex.extract()
