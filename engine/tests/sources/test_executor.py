"""The source executor for a DuckDB-attached SQLite file (ARCHITECTURE.md §5.5, §5.6).

Every test runs a real executor process. The slow query is a DuckDB `range` scan, which
`interrupt()` stops; a child stopped with SIGSTOP is how a query that ignores `interrupt()` looks
from the engine, so the kill path needs no test hook in the executor."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ontological.sources.executor import (
    MAX_ANSWER_ROWS,
    MAX_PROFILE_ROWS,
    Cancelled,
    ExecutorDied,
    History,
    SourceError,
    SourceExecutor,
    SourceQueryError,
    SqliteSource,
    StatementTimeout,
)

SLOW = "SELECT count(*) FROM range(100000000000000) a WHERE a.range % 7 = 3"

SCHEMA = """
CREATE TABLE artist (artist_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE album (
    album_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    artist_id INTEGER NOT NULL REFERENCES artist (artist_id)
);
CREATE TABLE playlist_track (
    playlist_id INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    album_id INTEGER REFERENCES album,
    PRIMARY KEY (playlist_id, track_id)
);
CREATE TABLE note (body TEXT, price NUMERIC(10, 2), played_at DATETIME);
CREATE VIEW album_titles AS SELECT title FROM album;
"""


def make_sqlite(path: Path, rows: int = 300) -> Path:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany(
        "INSERT INTO artist VALUES (?, ?)", [(i, f"artist {i}") for i in range(1, rows + 1)]
    )
    con.execute("INSERT INTO album VALUES (1, 'First', 1)")
    con.commit()
    con.close()
    return path


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_sqlite(tmp_path_factory.mktemp("src") / "chinookish.sqlite")


@pytest.fixture
def executor(db: Path) -> Iterator[SourceExecutor]:
    with SourceExecutor(SqliteSource(db)) as ex:
        yield ex


def run_in_thread(fn: Callable[[], object]) -> tuple[threading.Thread, dict[str, object]]:
    """Run `fn()` on a thread; the dict gets "result" or "error"."""
    out: dict[str, object] = {}

    def target() -> None:
        try:
            out["result"] = fn()
        except BaseException as e:
            out["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, out


def is_zombie(pid: int) -> bool:
    ps = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return ps.stdout.strip().startswith("Z")


def wait_running(ex: SourceExecutor) -> None:
    """Until the executor has an operation in flight."""
    deadline = time.monotonic() + 10
    while not ex.busy:
        assert time.monotonic() < deadline, "the operation never started"
        time.sleep(0.01)
    time.sleep(0.2)  # let the child start the query


# Extract


def test_extract_reads_the_catalog_in_upstream_s_shape(executor: SourceExecutor) -> None:
    cat = executor.extract()
    assert cat.database_name == "chinookish"
    assert cat.tables == [
        {"table_schema": "main", "table_name": "album", "table_type": "base table"},
        {"table_schema": "main", "table_name": "album_titles", "table_type": "view"},
        {"table_schema": "main", "table_name": "artist", "table_type": "base table"},
        {"table_schema": "main", "table_name": "note", "table_type": "base table"},
        {"table_schema": "main", "table_name": "playlist_track", "table_type": "base table"},
    ]
    album = [c for c in cat.columns if c["table_name"] == "album"]
    assert album == [
        {
            "table_schema": "main",
            "table_name": "album",
            "column_name": "album_id",
            "data_type": "BIGINT",
            "is_nullable": True,
            "ordinal_position": 1,
        },
        {
            "table_schema": "main",
            "table_name": "album",
            "column_name": "title",
            "data_type": "VARCHAR",
            "is_nullable": False,
            "ordinal_position": 2,
        },
        {
            "table_schema": "main",
            "table_name": "album",
            "column_name": "artist_id",
            "data_type": "BIGINT",
            "is_nullable": False,
            "ordinal_position": 3,
        },
    ]
    # Every listed relation is described, views included (upstream's coverage check).
    assert {c["table_name"] for c in cat.columns} == {t["table_name"] for t in cat.tables}
    assert cat.views == [
        {
            "table_schema": "main",
            "table_name": "album_titles",
            "view_definition": "CREATE VIEW album_titles AS SELECT title FROM album",
        }
    ]
    assert cat.skipped == []


def test_a_view_duckdb_can_t_read_is_skipped(tmp_path: Path) -> None:
    """SQLite reads a double-quoted "a*" as a string when no column has that name; DuckDB reads it
    as a column and sends SQLite back `"name" ~~~ "a*"`, which it can't parse. One such view must
    not stop the rest of the catalog."""
    db = make_sqlite(tmp_path / "glob.sqlite")
    con = sqlite3.connect(db)
    con.execute('CREATE VIEW globbed AS SELECT * FROM artist WHERE name GLOB "a*"')
    con.commit()
    con.close()
    with SourceExecutor(SqliteSource(db)) as ex:
        cat = ex.extract()
    assert "globbed" not in {t["table_name"] for t in cat.tables}
    assert "globbed" not in {v["table_name"] for v in cat.views}
    assert [(s["table_name"], "~~~" in s["error"]) for s in cat.skipped] == [("globbed", True)]
    assert {t["table_name"] for t in cat.tables} == {
        "album", "album_titles", "artist", "note", "playlist_track"
    }  # fmt: skip


def test_extract_reads_primary_keys_in_key_order(executor: SourceExecutor) -> None:
    pks = executor.extract().pks
    assert pks == [
        {"table_schema": "main", "table_name": "album", "column_name": "album_id",
         "ordinal_position": 1},
        {"table_schema": "main", "table_name": "artist", "column_name": "artist_id",
         "ordinal_position": 1},
        {"table_schema": "main", "table_name": "playlist_track", "column_name": "playlist_id",
         "ordinal_position": 1},
        {"table_schema": "main", "table_name": "playlist_track", "column_name": "track_id",
         "ordinal_position": 2},
    ]  # fmt: skip


def test_extract_reads_declared_foreign_keys(executor: SourceExecutor) -> None:
    """`REFERENCES album` names no column, so it means album's primary key."""
    assert executor.extract().fks == [
        {"table_schema": "main", "table_name": "album", "column_name": "artist_id",
         "referenced_schema": "main", "referenced_table": "artist",
         "referenced_column": "artist_id"},
        {"table_schema": "main", "table_name": "playlist_track", "column_name": "album_id",
         "referenced_schema": "main", "referenced_table": "album",
         "referenced_column": "album_id"},
    ]  # fmt: skip


# history


def test_a_source_without_query_history_yields_none(executor: SourceExecutor) -> None:
    """Only Postgres keeps a history the executor reads; a SQLite file has none."""
    assert executor.history() == History(dialects=[], rows=[])


# run_sql and probe


def test_run_sql_returns_columns_types_and_rows(executor: SourceExecutor) -> None:
    rows = executor.run_sql("SELECT artist_id, name FROM artist WHERE artist_id <= 2 ORDER BY 1")
    assert rows.columns == ["artist_id", "name"]
    assert rows.types == ["BIGINT", "VARCHAR"]
    assert rows.rows == [(1, "artist 1"), (2, "artist 2")]
    assert not rows.truncated


def test_run_sql_resolves_qualified_names_against_the_source(executor: SourceExecutor) -> None:
    assert executor.run_sql("SELECT title FROM main.album").rows == [("First",)]


def test_run_sql_keeps_at_most_200_rows(executor: SourceExecutor) -> None:
    assert MAX_ANSWER_ROWS == 200
    rows = executor.run_sql("SELECT artist_id FROM artist ORDER BY 1")
    assert len(rows.rows) == 200
    assert rows.rows[-1] == (200,)
    assert rows.truncated
    exact = executor.run_sql("SELECT artist_id FROM artist WHERE artist_id <= 200")
    assert len(exact.rows) == 200
    assert not exact.truncated


def test_probe_is_capped_by_max_rows(executor: SourceExecutor) -> None:
    rows = executor.probe("SELECT artist_id FROM artist", max_rows=5)
    assert len(rows.rows) == 5
    assert rows.truncated
    assert len(executor.probe("SELECT artist_id FROM artist").rows) == MAX_ANSWER_ROWS


def test_probe_takes_up_to_1000_profiling_rows(tmp_path: Path) -> None:
    assert MAX_PROFILE_ROWS == 1000
    with SourceExecutor(SqliteSource(make_sqlite(tmp_path / "big.db", rows=1500))) as ex:
        rows = ex.probe("SELECT artist_id FROM artist", max_rows=MAX_PROFILE_ROWS)
        assert len(rows.rows) == 1000
        assert rows.truncated
        with pytest.raises(ValueError):
            ex.probe("SELECT 1", max_rows=MAX_PROFILE_ROWS + 1)
        with pytest.raises(ValueError):
            ex.probe("SELECT 1", max_rows=0)


def test_an_empty_result_keeps_its_columns(executor: SourceExecutor) -> None:
    rows = executor.probe("SELECT name FROM artist WHERE false")
    assert rows.columns == ["name"]
    assert rows.rows == []
    assert not rows.truncated


def test_a_bad_query_raises_and_the_executor_carries_on(executor: SourceExecutor) -> None:
    pid = executor.pid
    with pytest.raises(SourceQueryError, match="no_such_table"):
        executor.run_sql("SELECT * FROM no_such_table")
    assert executor.run_sql("SELECT 1").rows == [(1,)]
    assert executor.pid == pid


# Read-only


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO artist VALUES (999, 'x')",
        "DELETE FROM artist",
        "CREATE TABLE memory.main.t AS SELECT 1",
        "SELECT 1; SELECT 2",
        "ATTACH '/tmp/other.db' AS other",
        "SET enable_external_access = true",
        "",
    ],
)
def test_only_one_select_runs(executor: SourceExecutor, db: Path, sql: str) -> None:
    with pytest.raises(SourceQueryError):
        executor.run_sql(sql)
    with pytest.raises(SourceQueryError):
        executor.probe(sql)
    assert executor.run_sql("SELECT count(*) FROM artist").rows == [(300,)]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/hosts')",
        "SELECT * FROM sqlite_scan('/etc/hosts', 'x')",
    ],
)
def test_a_select_can_t_reach_other_files(executor: SourceExecutor, sql: str) -> None:
    with pytest.raises(SourceQueryError, match="disabled"):
        executor.run_sql(sql)


def test_the_source_file_is_opened_read_only(db: Path) -> None:
    before = db.read_bytes()
    with SourceExecutor(SqliteSource(db)) as ex:
        ex.extract()
        ex.run_sql("SELECT * FROM artist")
    assert db.read_bytes() == before


# Timeouts and cancel


def test_a_timeout_interrupts_the_query_and_keeps_the_process(executor: SourceExecutor) -> None:
    pid = executor.pid
    started = time.monotonic()
    with pytest.raises(StatementTimeout):
        executor.probe(SLOW, timeout_s=0.3)
    assert time.monotonic() - started < 2
    assert executor.pid == pid
    assert executor.run_sql("SELECT 1").rows == [(1,)]


def test_cancel_interrupts_the_operation_in_flight(executor: SourceExecutor) -> None:
    pid = executor.pid
    t, out = run_in_thread(lambda: executor.run_sql(SLOW))
    wait_running(executor)
    started = time.monotonic()
    executor.cancel()
    t.join(5)
    assert isinstance(out.get("error"), Cancelled)
    assert time.monotonic() - started < 2
    assert executor.pid == pid
    assert executor.run_sql("SELECT 1").rows == [(1,)]


def test_cancel_with_nothing_in_flight_does_nothing(executor: SourceExecutor) -> None:
    executor.cancel()
    assert executor.run_sql("SELECT 1").rows == [(1,)]


def test_a_query_that_ignores_interrupt_is_killed_after_2_s(executor: SourceExecutor) -> None:
    pid = executor.pid
    assert pid is not None
    t, out = run_in_thread(lambda: executor.probe(SLOW, timeout_s=0.5))
    wait_running(executor)
    os.kill(pid, signal.SIGSTOP)  # deaf to interrupt(), as a native driver blocked in C is
    started = time.monotonic()
    t.join(10)
    waited = time.monotonic() - started
    assert isinstance(out.get("error"), StatementTimeout)
    assert 1.5 < waited < 4  # what was left of the 0.5 s timeout, then the 2 s grace
    assert executor.pid not in (None, pid)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert executor.run_sql("SELECT count(*) FROM artist").rows == [(300,)]


def test_a_cancel_that_ignores_interrupt_is_killed_after_2_s(executor: SourceExecutor) -> None:
    pid = executor.pid
    assert pid is not None
    t, out = run_in_thread(lambda: executor.run_sql(SLOW))
    wait_running(executor)
    os.kill(pid, signal.SIGSTOP)
    started = time.monotonic()
    executor.cancel()
    t.join(10)
    assert isinstance(out.get("error"), Cancelled)
    assert 1.5 < time.monotonic() - started < 4
    assert executor.pid not in (None, pid)
    assert executor.run_sql("SELECT 1").rows == [(1,)]


def test_a_process_that_dies_is_replaced(executor: SourceExecutor) -> None:
    pid = executor.pid
    assert pid is not None
    t, out = run_in_thread(lambda: executor.run_sql(SLOW))
    wait_running(executor)
    os.kill(pid, signal.SIGKILL)
    t.join(5)
    assert isinstance(out.get("error"), ExecutorDied)
    assert executor.run_sql("SELECT 1").rows == [(1,)]
    assert executor.pid != pid


def test_a_process_that_dies_while_idle_is_replaced(executor: SourceExecutor) -> None:
    pid = executor.pid
    assert pid is not None
    executor.run_sql("SELECT 1")
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while not is_zombie(pid):  # dead, and not yet reaped by the executor
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert executor.run_sql("SELECT 1").rows == [(1,)]
    assert executor.pid != pid
    assert executor.run_sql("SELECT count(*) FROM artist").rows == [(300,)]


def test_a_child_that_died_but_still_looks_alive_is_replaced(
    executor: SourceExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Linux a killed child whose threads are still exiting is a zombie `waitpid` can't reap
    yet, so `is_alive()` says yes and the operation's send is what finds it gone."""
    proc = executor._proc  # pyright: ignore[reportPrivateUsage]
    assert proc is not None and proc.pid is not None
    executor.run_sql("SELECT 1")
    os.kill(proc.pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while not is_zombie(proc.pid):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    monkeypatch.setattr(proc, "is_alive", lambda: True)

    assert executor.run_sql("SELECT 1").rows == [(1,)]
    assert executor.pid != proc.pid


def test_a_child_that_dies_before_it_starts_the_operation_is_replaced(
    executor: SourceExecutor,
) -> None:
    """Such a zombie can also keep its end of the pipe open: the send goes through, and the
    child is gone before it reads it. A stopped child takes the send the same way."""
    pid = executor.pid
    assert pid is not None
    executor.run_sql("SELECT 1")
    os.kill(pid, signal.SIGSTOP)
    killer = threading.Timer(0.3, os.kill, (pid, signal.SIGKILL))
    killer.start()

    assert executor.run_sql("SELECT 1").rows == [(1,)]
    killer.join()
    assert executor.pid != pid


# Lifecycle


def test_the_process_is_warm_before_the_first_operation(db: Path) -> None:
    with SourceExecutor(SqliteSource(db)) as ex:
        assert ex.pid is not None
        ex.wait_ready()
        started = time.monotonic()
        ex.run_sql("SELECT 1")
        assert time.monotonic() - started < 0.5


def test_a_missing_file_fails_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "gone.sqlite"
    with SourceExecutor(SqliteSource(missing)) as ex, pytest.raises(SourceError, match="gone"):
        ex.run_sql("SELECT 1")
    assert not missing.exists()


def test_a_file_that_isn_t_sqlite_fails(tmp_path: Path) -> None:
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database" * 100)
    with SourceExecutor(SqliteSource(junk)) as ex, pytest.raises(SourceError):
        ex.extract()


def test_close_stops_the_process(db: Path) -> None:
    ex = SourceExecutor(SqliteSource(db))
    pid = ex.pid
    assert pid is not None
    ex.run_sql("SELECT 1")
    ex.close()
    assert ex.pid is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(SourceError):
        ex.run_sql("SELECT 1")
