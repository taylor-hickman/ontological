"""The staleness check (ARCHITECTURE.md §9.1, BUILD_PLAN.md M2.6): ingest keeps each source on its
catalog database row, and the check re-ingests the sources last ingested over a day ago, through
real source executors into the bundled store, with no Claude call.

Needs ONTOLOGICAL_PG_BIN, like the rest of tests/store.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import sql

from ontological.catalog.ingest import ingest
from ontological.jobs.staleness import Refreshed, StalenessCheck, stale_sources
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.sources.spec import to_record
from ontological.store import pg


def copy_of(chinook: Path, tmp_path: Path, name: str) -> Path:
    """A copy of Chinook the test can change, named so it ingests as database `name`."""
    path = tmp_path / f"{name}.sqlite"
    shutil.copy(chinook, path)
    return path


def ingested(path: Path) -> SqliteSource:
    source = SqliteSource(path)
    with SourceExecutor(source) as ex:
        ingest(ex)
    return source


def pulled_ago(lay: pg.Layout, name: str, ago: timedelta | None) -> None:
    """Set a database's `pulled` stamp to `ago` before now, or clear it."""
    value = "NULL" if ago is None else f"now() - interval '{int(ago.total_seconds())} seconds'"
    sql(lay, f"UPDATE catalog_database SET pulled = {value} WHERE name = '{name}'")


def pulled(lay: pg.Layout, name: str) -> datetime:
    out = sql(lay, f"SELECT extract(epoch FROM pulled) FROM catalog_database WHERE name = '{name}'")
    return datetime.fromtimestamp(float(out), UTC)


def add_table(path: Path, name: str) -> None:
    with sqlite3.connect(path) as con:
        con.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY, label TEXT)')


def test_ingest_keeps_the_source_and_names_new_tables(
    store: pg.Layout, chinook: Path, tmp_path: Path
) -> None:
    path = copy_of(chinook, tmp_path, "chinook")
    source = SqliteSource(path)
    with SourceExecutor(source) as ex:
        first = ingest(ex)
        assert len(first.new_tables) == 11 and "main.Track" in first.new_tables
        assert ingest(ex).new_tables == []
        add_table(path, "Review")
        assert ingest(ex).new_tables == ["main.Review"]

    kept = sql(store, "SELECT connection FROM catalog_database WHERE name = 'chinook'")
    assert kept == f'{{"kind": "sqlite", "path": "{path}"}}'
    assert to_record(source) == {"kind": "sqlite", "path": str(path)}


def test_stale_sources_are_those_pulled_over_a_day_ago(
    store: pg.Layout, chinook: Path, tmp_path: Path
) -> None:
    stale = ingested(copy_of(chinook, tmp_path, "stale"))
    never = ingested(copy_of(chinook, tmp_path, "never"))
    ingested(copy_of(chinook, tmp_path, "fresh"))
    ingested(copy_of(chinook, tmp_path, "nosource"))
    pulled_ago(store, "stale", timedelta(hours=25))
    pulled_ago(store, "never", None)  # a row with a source and no finished ingest
    pulled_ago(store, "fresh", timedelta(hours=23))
    pulled_ago(store, "nosource", timedelta(hours=25))
    sql(store, "UPDATE catalog_database SET connection = NULL WHERE name = 'nosource'")

    assert stale_sources(datetime.now(UTC)) == [never, stale]


def test_the_check_refreshes_stale_sources_only_and_reports_new_tables(
    store: pg.Layout, chinook: Path, tmp_path: Path
) -> None:
    stale_path = copy_of(chinook, tmp_path, "stale")
    ingested(stale_path)
    ingested(copy_of(chinook, tmp_path, "fresh"))
    pulled_ago(store, "stale", timedelta(hours=25))
    pulled_ago(store, "fresh", timedelta(hours=1))
    fresh_before = pulled(store, "fresh")
    add_table(stale_path, "Review")
    started = datetime.now(UTC).replace(microsecond=0)

    got = StalenessCheck().run()

    assert got == [Refreshed("stale", ["main.Review"])]
    assert pulled(store, "stale") >= started
    assert pulled(store, "fresh") == fresh_before
    assert sql(store, "SELECT count(*) FROM catalog_table t JOIN catalog_schema s "
               "ON s.id = t.schema_id JOIN catalog_database d ON d.id = s.database_id "
               "WHERE d.name = 'stale'") == "12"  # fmt: skip
    assert sql(store, "SELECT count(*) FROM ops.llm_call") == "0", "a refresh costs no quota"


def test_a_source_that_fails_is_left_as_it_was_and_the_rest_still_refresh(
    store: pg.Layout, chinook: Path, tmp_path: Path
) -> None:
    gone = copy_of(chinook, tmp_path, "gone")
    ingested(gone)
    ingested(copy_of(chinook, tmp_path, "stale"))
    for name in ("gone", "stale"):
        pulled_ago(store, name, timedelta(days=3))
    gone_before = pulled(store, "gone")
    gone.unlink()

    got = StalenessCheck().run()

    assert [(r.database_name, r.error is None) for r in got] == [("gone", False), ("stale", True)]
    assert pulled(store, "gone") == gone_before
    assert pulled(store, "stale") > gone_before


def test_a_cancelled_check_refreshes_nothing_more(
    store: pg.Layout, chinook: Path, tmp_path: Path
) -> None:
    ingested(copy_of(chinook, tmp_path, "stale"))
    pulled_ago(store, "stale", timedelta(days=2))
    before = pulled(store, "stale")
    check = StalenessCheck()
    check.cancel()

    check.start()
    check.join()

    assert not check.running
    assert check.results == []
    assert pulled(store, "stale") == before
