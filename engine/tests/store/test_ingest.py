"""Minimal ingest (BUILD_PLAN.md M1.3): Chinook's catalog, extracted by a real source executor and
written through upstream's DAL to the bundled store, and re-ingested through the catalog diff (M2.3).
No query history.

Chinook is upstream's fixture SQL (MIT, THIRD_PARTY_NOTICES.md), built into a SQLite file here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import sql

from ontological.catalog import ingest as ingest_mod
from ontological.catalog.ingest import ingest
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

CHINOOK_SQL = Path(__file__).parents[1] / "upstream_fixtures/dev_tools/fixtures/sql/chinook.sql"
TABLES = [
    "Album", "Artist", "Customer", "Employee", "Genre", "Invoice", "InvoiceLine", "MediaType",
    "Playlist", "PlaylistTrack", "Track",
]  # fmt: skip


@pytest.fixture(scope="module")
def chinook(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("src") / "chinook.sqlite"
    con = sqlite3.connect(path)
    try:
        con.executescript(CHINOOK_SQL.read_text(encoding="utf-8-sig"))  # the script has a BOM
    finally:
        con.close()
    return path


@pytest.fixture
def executor(chinook: Path) -> Iterator[SourceExecutor]:
    with SourceExecutor(SqliteSource(chinook)) as ex:
        yield ex


def rows(lay: pg.Layout, q: str) -> list[str]:
    out = sql(lay, q)
    return out.splitlines() if out else []


def catalog(lay: pg.Layout) -> list[str]:
    """Every catalog row, with its id, as `database|schema|table|column|id`."""
    return rows(
        lay,
        "SELECT d.name, s.name, t.name || '|' || t.id, c.name || '|' || c.id "
        "FROM catalog_database d JOIN catalog_schema s ON s.database_id = d.id "
        "JOIN catalog_table t ON t.schema_id = s.id JOIN catalog_column c ON c.table_id = t.id "
        "ORDER BY 1, 2, 3, 4",
    )


def keys(lay: pg.Layout) -> list[str]:
    """Every table's primary key and every foreign key, by id."""
    return rows(
        lay, "SELECT id || '|' || array_to_string(pk, ',') FROM catalog_table ORDER BY 1"
    ) + rows(
        lay,
        "SELECT source_column_id || '->' || target_column_id FROM column__foreign_key ORDER BY 1",
    )


def test_ingest_writes_chinook_s_tables_and_columns(
    store: pg.Layout, executor: SourceExecutor, chinook: Path
) -> None:
    got = ingest(executor)

    assert (got.database_name, got.tables, got.columns, got.skipped) == ("chinook", 11, 64, [])
    assert rows(store, "SELECT id FROM catalog_database WHERE name = 'chinook'") == [
        got.database_id
    ]
    assert rows(store, "SELECT name FROM catalog_schema") == ["main"]
    assert rows(store, "SELECT name || '|' || table_type FROM catalog_table ORDER BY name") == [
        f"{t}|base table" for t in TABLES
    ]
    with sqlite3.connect(chinook) as con:
        in_source = con.execute(
            "SELECT count(*) FROM sqlite_master m, pragma_table_info(m.name) WHERE m.type = 'table'"
        ).fetchone()
    assert rows(store, "SELECT count(*) FROM catalog_column") == [str(in_source[0])]
    # DuckDB's types, since DuckDB runs the queries; SQLite's nullability and column order
    assert rows(
        store,
        "SELECT c.name || '|' || c.data_type || '|' || c.is_nullable || '|' || c.ordinal_position "
        "FROM catalog_column c JOIN catalog_table t ON t.id = c.table_id "
        "WHERE t.name = 'Track' ORDER BY c.ordinal_position",
    ) == [
        "TrackId|BIGINT|false|1",
        "Name|VARCHAR|false|2",
        "AlbumId|BIGINT|true|3",
        "MediaTypeId|BIGINT|false|4",
        "GenreId|BIGINT|true|5",
        "Composer|VARCHAR|true|6",
        "Milliseconds|BIGINT|false|7",
        "Bytes|BIGINT|true|8",
        "UnitPrice|DOUBLE|false|9",
    ]
    assert rows(store, "SELECT pulled IS NOT NULL FROM catalog_database") == ["t"]


def test_ingest_writes_primary_and_foreign_keys(store: pg.Layout, executor: SourceExecutor) -> None:
    ingest(executor)

    pks = rows(store, "SELECT name || '|' || array_to_string(pk, ',') FROM catalog_table")
    keys = {t: f"{t}Id" for t in TABLES} | {"PlaylistTrack": "PlaylistId,TrackId"}
    assert sorted(pks) == sorted(f"{t}|{k}" for t, k in keys.items())
    fks = rows(
        store,
        "SELECT st.name || '.' || sc.name || ' -> ' || tt.name || '.' || tc.name "
        "FROM column__foreign_key f "
        "JOIN catalog_column sc ON sc.id = f.source_column_id "
        "JOIN catalog_table st ON st.id = sc.table_id "
        "JOIN catalog_column tc ON tc.id = f.target_column_id "
        "JOIN catalog_table tt ON tt.id = tc.table_id ORDER BY 1",
    )
    assert fks == [
        "Album.ArtistId -> Artist.ArtistId",
        "Customer.SupportRepId -> Employee.EmployeeId",
        "Employee.ReportsTo -> Employee.EmployeeId",  # Chinook's self-reference
        "Invoice.CustomerId -> Customer.CustomerId",
        "InvoiceLine.InvoiceId -> Invoice.InvoiceId",
        "InvoiceLine.TrackId -> Track.TrackId",
        "PlaylistTrack.PlaylistId -> Playlist.PlaylistId",
        "PlaylistTrack.TrackId -> Track.TrackId",
        "Track.AlbumId -> Album.AlbumId",
        "Track.GenreId -> Genre.GenreId",
        "Track.MediaTypeId -> MediaType.MediaTypeId",
    ]


def test_ingest_reads_no_query_history(store: pg.Layout, executor: SourceExecutor) -> None:
    ingest(executor)

    for table in ("sql_query", "sql_query__table", "sql_query__column", "column__join"):
        assert rows(store, f"SELECT count(*) FROM {table}") == ["0"], table


def test_re_ingesting_an_unchanged_source_changes_nothing(
    store: pg.Layout, executor: SourceExecutor
) -> None:
    """Re-ingest is idempotent (ARCHITECTURE.md §12, M2): the second ingest diffs against the
    first and finds nothing to do, so every row keeps its id and the keys stay as they were."""
    first = ingest(executor)
    before = catalog(store), keys(store)

    second = ingest(executor)

    assert (second.database_id, second.tables, second.columns) == (
        first.database_id, first.tables, first.columns,
    )  # fmt: skip
    assert (catalog(store), keys(store)) == before


def test_a_failed_write_leaves_no_catalog_and_can_be_retried(
    store: pg.Layout, executor: SourceExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_: object) -> None:
        raise RuntimeError("the store went away")

    with monkeypatch.context() as patch:
        patch.setattr(ingest_mod, "add_pks", fail)  # the last write before `pulled`
        with pytest.raises(RuntimeError, match="went away"):
            ingest(executor)
    for table in ("catalog_database", "catalog_schema", "catalog_table", "catalog_column"):
        assert rows(store, f"SELECT count(*) FROM {table}") == ["0"], table

    assert ingest(executor).tables == 11


def test_a_source_with_no_tables_writes_nothing(store: pg.Layout, tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    with SourceExecutor(SqliteSource(empty)) as ex:
        got = ingest(ex)

    assert (got.database_id, got.tables, got.columns) == (None, 0, 0)
    assert rows(store, "SELECT count(*) FROM catalog_database") == ["0"]
