"""The staleness check (ARCHITECTURE.md §9.1, BUILD_PLAN.md M2.6): ingest keeps each source on its
catalog database row, and the check re-ingests the sources last ingested over a day ago, through
real source executors into the bundled store, with no Claude call.

Needs ONTOLOGICAL_PG_BIN, like the rest of tests/store.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import sql
from fakes import NoAnswer, ScriptedGateway
from test_jobs_screen import WaitingClock, state, until

from ontological import settings
from ontological.catalog.ingest import ingest
from ontological.jobs import store as jobs
from ontological.jobs.active import ActiveJobs
from ontological.jobs.service import Jobs, auto_compile_from
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
    # Recorded for the Jobs screen's "1 new table in stale" prompt
    [job] = jobs.recent(5)
    assert (job.kind, job.database_name, job.state, job.error) == ("ingest", "stale", "done", None)
    assert job.result == {"new_tables": ["main.Review"]}
    assert job.created_at >= started and job.finished_at is not None
    assert job.dismissed_at is None
    jobs.dismiss(job.id)
    assert jobs.job(job.id).dismissed_at is not None


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
    # Both are recorded; two failed refreshes of one source don't collide
    StalenessCheck().run()
    got_jobs = [(j.database_name, j.state, j.error is not None) for j in reversed(jobs.recent(5))]
    assert got_jobs == [
        ("gone", "stopped", True),
        ("stale", "done", False),
        ("gone", "stopped", True),
    ]


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


def limited(task: str, user: str) -> NoAnswer:
    """Claude at its usage limit, so a compile it is asked for waits, paused, until the engine
    stops."""
    return NoAnswer("limited", "limited", datetime.now(UTC) + timedelta(hours=1))


def refresh_with_jobs() -> tuple[list[bool], jobs.Job]:
    """The engine's staleness check over Chinook's copy `stale`, handing what it finds to the
    engine's Jobs as `_serve` does. Whether each refresh it handed over started a compile, and
    the refresh's job."""
    handed: list[bool] = []

    async def scenario() -> None:
        service = Jobs(gateway=ScriptedGateway(limited), active=ActiveJobs(), clock=WaitingClock())
        auto_compile = auto_compile_from(service, asyncio.get_running_loop())
        check = StalenessCheck(new_tables=lambda refresh: handed.append(auto_compile(refresh)))
        await asyncio.to_thread(check.run)
        if any(handed):
            await until(lambda: state("stale") == "paused")
        await service.stop()

    asyncio.run(scenario())
    [refresh] = [j for j in jobs.recent(5) if j.kind == "ingest"]
    return handed, refresh


def stale_chinook(lay: pg.Layout, chinook: Path, tmp_path: Path, *, new_table: bool) -> None:
    path = copy_of(chinook, tmp_path, "stale")
    ingested(path)
    pulled_ago(lay, "stale", timedelta(hours=25))
    if new_table:
        add_table(path, "Review")


def test_with_auto_compile_on_a_refresh_with_new_tables_starts_their_compile(
    store: pg.Layout, chinook: Path, tmp_path: Path
) -> None:
    stale_chinook(store, chinook, tmp_path, new_table=True)
    settings.put(settings.AUTO_COMPILE, True)

    handed, refresh = refresh_with_jobs()

    assert handed == [True]
    assert state("stale") == "paused", "a compile of stale started by itself"
    assert refresh.result == {"new_tables": ["main.Review"]}
    assert refresh.dismissed_at is not None, "the compile answers the refresh's prompt"


@pytest.mark.parametrize("setting", [None, False], ids=["unset", "off"])
def test_with_auto_compile_off_a_refresh_with_new_tables_waits_for_the_person(
    store: pg.Layout, chinook: Path, tmp_path: Path, setting: bool | None
) -> None:
    stale_chinook(store, chinook, tmp_path, new_table=True)
    if setting is not None:
        settings.put(settings.AUTO_COMPILE, setting)
    assert settings.auto_compile() is False

    handed, refresh = refresh_with_jobs()

    assert handed == [False]
    assert state("stale") is None
    assert refresh.result == {"new_tables": ["main.Review"]}
    assert refresh.dismissed_at is None, "Jobs still asks: N new tables, compile?"


def test_with_auto_compile_on_a_refresh_with_no_new_tables_compiles_nothing(
    store: pg.Layout, chinook: Path, tmp_path: Path
) -> None:
    stale_chinook(store, chinook, tmp_path, new_table=False)
    settings.put(settings.AUTO_COMPILE, True)

    handed, refresh = refresh_with_jobs()

    assert handed == [], "nothing to hand over"
    assert state("stale") is None
    assert refresh.result == {"new_tables": []}
