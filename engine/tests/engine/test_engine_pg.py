"""The engine on the bundled Postgres: it starts the store and brings both Alembic chains to their
heads before it serves, and an idle shutdown writes the quit dump and stops the server
(ARCHITECTURE.md §4.4, §5.3).

Needs ONTOLOGICAL_PG_BIN, as the store tests do (see tests/store/conftest.py); skips without it
unless ONTOLOGICAL_PG_REQUIRED=1.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from engine_proc import Proc, app_data, http

from ontological.catalog.ingest import ingest
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import chains, dal, pg

__all__ = ["app_data"]  # the fixture


@pytest.fixture
def pg_bin() -> Path:
    value = os.environ.get("ONTOLOGICAL_PG_BIN")
    if not value:
        if os.environ.get("ONTOLOGICAL_PG_REQUIRED") == "1":
            pytest.fail("ONTOLOGICAL_PG_BIN is not set and ONTOLOGICAL_PG_REQUIRED=1")
        pytest.skip("set ONTOLOGICAL_PG_BIN to a PostgreSQL 17 bin/ directory")
    return Path(value).resolve()


def stop_leftover_server(layout: pg.Layout) -> None:
    pf = pg.read_pid_file(layout.pgdata)
    if pf and pg.pid_alive(pf.pid) and pg.is_postgres(pf.pid):
        os.kill(pf.pid, signal.SIGQUIT)
        with contextlib.suppress(pg.PgError):
            pg.wait_gone(pf.pid, 30)


def engine(app_data: Path, pg_bin: Path, *args: str) -> Proc:
    return Proc(
        [
            sys.executable, "-m", "ontological.engine", "--app-data", str(app_data),
            "--pg-bin", str(pg_bin), *args,
        ]
    )  # fmt: skip


def test_the_engine_migrates_a_store_that_is_behind(app_data: Path, pg_bin: Path) -> None:
    layout = pg.Layout(app_data, pg_bin)
    pg.ensure_running(layout, prepare_store=None)  # a store with neither chain applied
    assert chains.pending(pg.url(layout)) == ["upstream", "ontological"]
    pg.stop(layout, quit_dump=False)
    e = engine(app_data, pg_bin)
    try:
        assert e.info["started"]
        assert chains.pending(pg.url(layout)) == []
        assert len(list(layout.backups.glob("ontology-*-premigrate.dump"))) == 1
        assert e.term() == 0
    finally:
        stop_leftover_server(layout)


def test_the_engine_runs_the_store_and_idles_out_with_a_quit_dump(
    app_data: Path, pg_bin: Path
) -> None:
    layout = pg.Layout(app_data, pg_bin)
    e = engine(app_data, pg_bin, "--idle-timeout", "2")
    try:
        assert e.info["started"]
        state = pg.inspect_pid_file(layout)
        assert isinstance(state, pg.Running), "the store is up before the engine serves"
        postmaster = state.pid_file.pid
        assert http(app_data, "GET", "/health")[0] == 200
        assert chains.pending(pg.url(layout)) == []

        assert e.wait_exit(60) == 0
        assert not pg.pid_alive(postmaster)
        assert pg.read_pid_file(layout.pgdata) is None, "a clean stop removes postmaster.pid"
        dumps = sorted(layout.backups.glob("ontology-*-quit.dump"))
        assert len(dumps) == 1 and dumps[0].stat().st_size > 0
        assert not (app_data / "engine.json").exists()
    finally:
        stop_leftover_server(layout)


def test_the_engine_refreshes_a_source_last_ingested_over_a_day_ago(
    app_data: Path, pg_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staleness check (§9.1): once the engine serves, it re-ingests the stale source and
    finds the table added since, while the engine answers requests."""
    layout = pg.Layout(app_data, pg_bin)
    shop = tmp_path / "shop.sqlite"
    with sqlite3.connect(shop) as con:
        con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL)")
    pg.ensure_running(layout)
    for name in dal.environ(layout):
        monkeypatch.delenv(name, raising=False)  # dal.connect sets them; put them back after
    dal.connect(layout)
    try:
        with SourceExecutor(SqliteSource(shop)) as ex:
            ingest(ex)
    finally:
        dal.close()
    pg.psql(layout, "UPDATE catalog_database SET pulled = now() - interval '25 hours'")
    pg.stop(layout, quit_dump=False)
    with sqlite3.connect(shop) as con:
        con.execute("CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER)")

    e = engine(app_data, pg_bin)
    try:
        assert http(app_data, "GET", "/health")[0] == 200
        tables = "SELECT string_agg(name, ',' ORDER BY name) FROM catalog_table"
        deadline = time.monotonic() + 60
        while pg.psql(layout, tables) != "orders,refunds":
            assert time.monotonic() < deadline, "the stale source wasn't refreshed"
            time.sleep(0.2)
        age = "SELECT now() - pulled < interval '1 minute' FROM catalog_database"
        assert pg.psql(layout, age) == "t"
        assert e.term() == 0
    finally:
        stop_leftover_server(layout)
