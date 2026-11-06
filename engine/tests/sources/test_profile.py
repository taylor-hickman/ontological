"""Profiling inside the source executor (ARCHITECTURE.md §1.3 stages 1 and 2, §5.6, §11).

Each test profiles a real source through a real executor process, with the catalog's column types
as extract reports them, since those are what the engine passes in."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from ontological.sources.executor import (
    DuckDBSource,
    FileSource,
    FileTable,
    SourceExecutor,
    SourceQueryError,
    SqliteSource,
    StatementTimeout,
)


def catalog_columns(ex: SourceExecutor, schema: str, table: str) -> list[dict[str, Any]]:
    """The `name` and `data_type` of each column, as the store holds them after ingest."""
    return [
        {"name": c["column_name"], "data_type": c["data_type"]}
        for c in ex.extract().columns
        if (c["table_schema"], c["table_name"]) == (schema, table)
    ]


# SQLite: dates stored as text, and an enum value the first 1,000 rows miss


@pytest.fixture(scope="module")
def tickets(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("sqlite") / "support.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE ticket (id INTEGER PRIMARY KEY, status TEXT, opened TEXT, "
        "priority INTEGER, note TEXT)"
    )
    rows = [
        (
            i,
            "banned" if i == 1500 else ("closed" if i % 3 == 0 else "open"),
            f"{i % 28 + 1:02d}/{i % 12 + 1:02d}/2024",  # days past the 12th: day-first
            None if i % 4 == 0 else i % 3,
            "a long free-text note that runs well past thirty characters",
        )
        for i in range(1, 2001)
    ]
    con.executemany("INSERT INTO ticket VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return path


@pytest.fixture
def ticket_ex(tickets: Path) -> Iterator[SourceExecutor]:
    with SourceExecutor(SqliteSource(tickets)) as ex:
        yield ex


def test_profile_reads_uniqueness_samples_and_formats(ticket_ex: SourceExecutor) -> None:
    got = ticket_ex.profile("main", "ticket", catalog_columns(ticket_ex, "main", "ticket"))

    assert got["uniqueness"] == {
        "id": True,
        "status": False,
        "opened": False,
        "priority": False,
        "note": False,
    }
    assert got["date_formats"] == {"opened": "DD/MM/YYYY"}
    # Integers stay integers through a column with NULLs.
    assert got["sample_values"]["priority"] == [1, 2, 0]
    assert all(type(v) is int for v in got["sample_values"]["priority"])
    # A text date keeps its samples beside its format.
    assert len(got["sample_values"]["opened"]) == 5
    # Prose is profiled but not kept as a sample.
    assert "note" not in got["sample_values"]
    assert got["columns"]["note"]["sample_values"] == [
        "a long free-text note that runs well past thirty characters"
    ]


def test_profile_finds_an_enum_value_the_row_sample_misses(ticket_ex: SourceExecutor) -> None:
    columns = catalog_columns(ticket_ex, "main", "ticket")

    probed = ticket_ex.profile("main", "ticket", columns)
    unprobed = ticket_ex.profile("main", "ticket", columns, probe_distinct_values=False)

    assert probed["sample_values"]["status"] == ["open", "closed", "banned"]
    assert unprobed["sample_values"]["status"] == ["open", "closed"]


def test_a_table_the_source_lacks_raises_and_the_executor_carries_on(
    ticket_ex: SourceExecutor,
) -> None:
    with pytest.raises(SourceQueryError, match="nope"):
        ticket_ex.profile("main", "nope", [])
    assert ticket_ex.run_sql("SELECT count(*) FROM ticket").rows == [(2000,)]


# DuckDB: JSON, DATE and UUID columns, in a schema other than main


@pytest.fixture(scope="module")
def events(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("duck") / "events.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        """
        CREATE SCHEMA app;
        CREATE TABLE app.event (
            id UUID, happened DATE, payload JSON, "Kind Of Event" VARCHAR
        );
        INSERT INTO app.event
        SELECT uuid(), DATE '2024-01-01' + range::INT,
            CASE WHEN range % 2 = 0
                THEN '{"tier": "gold", "salary": {"value": 45000, "unit": "annual"}}'
                ELSE '{"tier": "silver"}' END,
            'click'
        FROM range(10);
        CREATE VIEW app.slow AS
            SELECT count(*) AS n FROM range(100000000000000) a WHERE a.range % 7 = 3;
        """
    )
    con.close()
    return path


@pytest.fixture
def event_ex(events: Path) -> Iterator[SourceExecutor]:
    with SourceExecutor(DuckDBSource(events)) as ex:
        yield ex


def test_profile_reads_json_keys_and_leaves_dates_and_uuids_out(event_ex: SourceExecutor) -> None:
    got = event_ex.profile("app", "event", catalog_columns(event_ex, "app", "event"))

    assert got["sample_values"]["payload"] == [
        "salary.value [unit: salary.unit]",
        "salary.unit",
        "tier [e.g. 'gold', 'silver']",
    ]
    assert got["date_formats"] == {"happened": "YYYY-MM-DD"}
    assert "happened" not in got["sample_values"]
    assert "id" not in got["sample_values"]
    assert got["uniqueness"]["id"] is True
    # A quoted name with spaces reaches its own DISTINCT probe.
    assert got["sample_values"]["Kind Of Event"] == ["click"]


def test_a_profile_past_its_timeout_is_stopped(event_ex: SourceExecutor) -> None:
    pid = event_ex.pid
    started = time.monotonic()
    with pytest.raises(StatementTimeout):
        event_ex.profile("app", "slow", [{"name": "n", "data_type": "BIGINT"}], timeout_s=0.3)
    assert time.monotonic() - started < 2
    assert event_ex.pid == pid
    assert event_ex.profile("app", "event", [])["uniqueness"]["happened"] is True


# Files: each is a view in the session's own catalog


def test_profile_reads_a_file_source(tmp_path: Path) -> None:
    path = tmp_path / "payments.csv"
    path.write_text("id,paid_on,method\n1,2026-01-01,card\n2,2026-01-02,cash\n3,2026-01-03,card\n")
    with SourceExecutor(FileSource("money", (FileTable.of(path),))) as ex:
        got = ex.profile("main", "payments", catalog_columns(ex, "main", "payments"))

    assert got["date_formats"] == {"paid_on": "YYYY-MM-DD"}  # a DATE, as DuckDB reads the CSV
    assert got["sample_values"] == {"id": [1, 2, 3], "method": ["card", "cash"]}
