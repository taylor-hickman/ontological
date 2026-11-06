"""`ingest` (BUILD_PLAN.md M2.9): WWI from the eval cluster through DuckDB's Postgres scanner and a
BIRD database through SQLite attach, each by a real source executor into the eval store.

WWI is the small WWI-shaped dataset in conftest.py, and `financial` a SQLite file made here with
BIRD's habits: a column name with a space, and a foreign key that names no column.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from ontological.engine import discovery
from ontological.store import pg

from ontological_eval import cluster, ingest
from ontological_eval.home import bird_dir

FINANCIAL = """
    CREATE TABLE district (district_id INTEGER PRIMARY KEY, "A2 name" TEXT);
    CREATE TABLE account (
        account_id INTEGER PRIMARY KEY,
        district_id INTEGER NOT NULL REFERENCES district,
        date DATE
    );
    INSERT INTO district VALUES (1, 'Hl.m. Praha');
    INSERT INTO account VALUES (10, 1, '1995-03-24');
"""


@pytest.fixture
def bird(home: Path) -> Path:
    path = bird_dir(home) / "financial.sqlite"
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(FINANCIAL)
    finally:
        con.close()
    return path


def store_sql(home: Path, pg_bin: Path, q: str) -> list[str]:
    lay = ingest.layout(home, pg_bin)
    pg.ensure_running(lay)
    out = pg.psql(lay, q)
    return out.splitlines() if out else []


def catalog(home: Path, pg_bin: Path) -> list[str]:
    """`database|schema|table|type|column` for every column in the eval store."""
    return store_sql(
        home,
        pg_bin,
        "SELECT d.name || '|' || s.name || '|' || t.name || '|' || t.table_type || '|' || c.name "
        "FROM catalog_database d JOIN catalog_schema s ON s.database_id = d.id "
        "JOIN catalog_table t ON t.schema_id = s.id JOIN catalog_column c ON c.table_id = t.id "
        "ORDER BY 1",
    )


def test_ingest_writes_wwi_and_a_bird_database_to_the_eval_store(
    home: Path, pg_bin: Path, bird: Path
) -> None:
    results = ingest.run(home, pg_bin, ["wwi", "financial"])

    got = {r.name: r.ingested for r in results}
    assert got["wwi"] is not None and got["financial"] is not None
    assert (got["wwi"].database_name, got["wwi"].tables, got["wwi"].skipped) == (
        "wideworldimporters",
        3,
        [],
    )
    assert (got["financial"].database_name, got["financial"].tables) == ("financial", 2)
    assert catalog(home, pg_bin) == [
        "financial|main|account|base table|account_id",
        "financial|main|account|base table|date",
        "financial|main|account|base table|district_id",
        "financial|main|district|base table|A2 name",
        "financial|main|district|base table|district_id",
        "wideworldimporters|sales|orderlines|base table|orderid",
        "wideworldimporters|sales|orderlines|base table|orderlineid",
        "wideworldimporters|sales|ordersizes|view|n",  # the store sorts '|' after 's'
        "wideworldimporters|sales|ordersizes|view|orderid",
        "wideworldimporters|sales|orders|base table|comments",
        "wideworldimporters|sales|orders|base table|orderid",
    ]
    assert store_sql(
        home,
        pg_bin,
        "SELECT st.name || '.' || sc.name || ' -> ' || tt.name || '.' || tc.name "
        "FROM column__foreign_key f "
        "JOIN catalog_column sc ON sc.id = f.source_column_id "
        "JOIN catalog_table st ON st.id = sc.table_id "
        "JOIN catalog_column tc ON tc.id = f.target_column_id "
        "JOIN catalog_table tt ON tt.id = tc.table_id ORDER BY 1",
    ) == ["account.district_id -> district.district_id", "orderlines.orderid -> orders.orderid"]
    assert store_sql(
        home, pg_bin, "SELECT name || '|' || array_to_string(pk, ',') FROM catalog_table "
        "WHERE pk IS NOT NULL ORDER BY 1",
    ) == ["account|account_id", "district|district_id", "orders|orderid"]  # fmt: skip


def test_ingest_stops_the_servers_it_started(home: Path, pg_bin: Path, bird: Path) -> None:
    ingest.run(home, pg_bin, ["wwi", "financial"])

    assert cluster.running(home, pg_bin) is None
    assert not isinstance(pg.inspect_pid_file(ingest.layout(home, pg_bin)), pg.Running)


def test_ingest_leaves_running_the_servers_it_found_running(home: Path, pg_bin: Path) -> None:
    eval_cluster = cluster.up(home, pg_bin)
    store = pg.ensure_running(ingest.layout(home, pg_bin))

    ingest.run(home, pg_bin, ["wwi"])

    still = cluster.running(home, pg_bin)
    assert still is not None and still.pid == eval_cluster.postmaster_pid
    state = pg.inspect_pid_file(ingest.layout(home, pg_bin))
    assert isinstance(state, pg.Running) and state.pid_file.pid == store.postmaster_pid


def test_a_second_ingest_leaves_the_store_as_it_is(home: Path, pg_bin: Path, bird: Path) -> None:
    ingest.run(home, pg_bin, ["wwi", "financial"])
    before = catalog(home, pg_bin)

    again = ingest.run(home, pg_bin, ["financial", "wwi"])

    assert [(r.name, r.ingested) for r in again] == [("financial", None), ("wwi", None)]
    assert catalog(home, pg_bin) == before


def test_ingest_refuses_while_an_engine_serves_the_store(
    home: Path, pg_bin: Path, bird: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = discovery.EngineInfo(pid=4242, port=1, token="t", version="0")

    def find_engine(_: Path) -> discovery.EngineInfo:
        return live

    monkeypatch.setattr(discovery, "find_engine", find_engine)

    with pytest.raises(ingest.IngestError, match="pid 4242"):
        ingest.run(home, pg_bin, ["financial"])

    assert not ingest.layout(home, pg_bin).pgdata.exists()


def test_ingest_refuses_an_unknown_or_missing_database(home: Path, pg_bin: Path) -> None:
    with pytest.raises(ingest.IngestError, match="no eval database 'chinook'"):
        ingest.run(home, pg_bin, ["chinook"])
    with pytest.raises(ingest.IngestError, match="fetch bird-databases"):
        ingest.run(home, pg_bin, ["wwi", "formula_1"])

    assert not ingest.layout(home, pg_bin).app_data.exists()
    assert cluster.running(home, pg_bin) is None
