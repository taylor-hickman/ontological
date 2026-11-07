"""The pre-compile estimate (BUILD_PLAN.md M3.7, ARCHITECTURE.md §6.5) on Chinook in the bundled
store: read from the catalog before anything is asked, it counts the calls a compile then makes,
for split and for merged, and after a stop it counts only what the job has left."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from fakes import NoAnswer, ScriptedGateway
from test_compile import claude, tables_of

from ontological.catalog.ingest import ingest
from ontological.compile.estimate import estimate_database, tables_to_compile
from ontological.compile.packing import TableCalls
from ontological.compile.split import compile_database
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg


@pytest.mark.parametrize("table_calls", ["split", "merged"])
def test_a_compile_makes_the_calls_its_estimate_counted(
    store: pg.Layout, chinook: Path, table_calls: TableCalls
) -> None:
    with SourceExecutor(SqliteSource(chinook)) as ex:
        ingest(ex)
        before = estimate_database("chinook")
        gateway = ScriptedGateway(claude)
        asyncio.run(compile_database("chinook", ex, gateway, table_calls=table_calls))

    option = before.split if table_calls == "split" else before.merged
    made = len(gateway.calls)
    assert before.tables == 11
    # The script leaves Genre out of a packed answer and answers its merged call invalidly, so
    # compile asks it again: what the range leaves room for
    assert option.calls < made <= option.calls_high, (table_calls, option, Counter(gateway.calls))
    assert option.usd_low < option.usd < option.usd_high
    assert option.seconds_low < option.seconds < option.seconds_high
    # Split: keys, descriptions and Terms for the small tables' pack and each wide table, and
    # the bridge's SQL for PlaylistTrack. Merged: a call for each table but PlaylistTrack, which
    # has nothing to ask, and the same bridge
    assert before.split.calls_by_model == {"haiku": 5, "opus": 1, "sonnet": 10}
    assert before.merged.calls_by_model == {"opus": 1, "sonnet": 10}
    # The next compile would plan only PlaylistTrack, which has no Term and nothing to ask, and
    # its bridge is written: no calls
    after = estimate_database("chinook")
    assert after.tables == 1 and after.split.calls == after.merged.calls == 0


def test_after_a_stop_only_the_job_s_pending_tables_are_estimated(
    store: pg.Layout, chinook: Path
) -> None:
    def stops_at_employee(task: str, user: str) -> dict[str, Any] | NoAnswer | None:
        if "Employee" in tables_of(user):
            return NoAnswer("error", "needs_login")
        return claude(task, user)

    with SourceExecutor(SqliteSource(chinook)) as ex:
        ingest(ex)
        got = asyncio.run(compile_database("chinook", ex, ScriptedGateway(stops_at_employee)))

    assert got.stopped is not None
    left = {t.name for t in tables_to_compile("chinook")}
    assert left == {"Employee", "Invoice", "Track"}
    assert estimate_database("chinook").tables == 3
