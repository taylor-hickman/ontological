"""Compile as a job (BUILD_PLAN.md M3.1, M3.2; ARCHITECTURE.md §4.4, §6.5): each table
checkpointed in ops.job_step, a pack's tables together, a pause until `resets_at` on a rate limit,
and a resume on the plan the job began with, after a pause, a stop or a crash. Chinook, ingested
into the bundled store, with a scripted gateway and a clock whose sleeps return at once."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import T0, FakeClock, NoAnswer, ScriptedGateway
from test_compile import ORDER, PACK_ORDER, SMALL, claude, rows, table_of, tables_of

from ontological.catalog.ingest import ingest
from ontological.compile.split import RESUME_MARGIN_S, RETRY_WITHOUT_RESET_S, compile_database
from ontological.engine.lifetime import Lifetime
from ontological.jobs import store as jobs
from ontological.jobs.active import ActiveJobs
from ontological.llm.calls import CallScope, current_scope
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

RESETS_AT = T0 + timedelta(hours=2)
# The tables a compile writes before it gets to Employee: the small tables' pack, then Customer
BEFORE_EMPLOYEE = {*SMALL, "Customer"}
# The plan for Chinook's tables: the small ones share pack 0 (tests/compile/test_packing.py)
PACKS = {
    "Album": 0, "Artist": 0, "Customer": 1, "Employee": 2, "Genre": 0, "Invoice": 3,
    "InvoiceLine": 0, "MediaType": 0, "Playlist": 0, "PlaylistTrack": 0, "Track": 4,
}  # fmt: skip


@pytest.fixture
def executor(store: pg.Layout, chinook: Path) -> Any:
    with SourceExecutor(SqliteSource(chinook)) as ex:
        ingest(ex)
        yield ex


def once_at_employee(no_answer: NoAnswer) -> Callable[[str, str], Any]:
    """Claude, except that the first `table_terms` call for Employee gets no answer."""
    seen: list[bool] = []

    def answer(task: str, user: str) -> Any:
        if task == "table_terms" and table_of(user) == "Employee" and not seen:
            seen.append(True)
            return no_answer
        return claude(task, user)

    return answer


def step_states(job_id: int) -> list[tuple[str, str, int]]:
    return [(s.table_name, s.state, s.pack) for s in jobs.job_steps(job_id)]


def test_each_table_is_checkpointed_with_its_pack(
    store: pg.Layout, executor: SourceExecutor
) -> None:
    clock = FakeClock()

    got = asyncio.run(compile_database("chinook", executor, ScriptedGateway(claude), clock=clock))

    job = jobs.job(got.job_id)
    assert (job.kind, job.database_name, job.state, job.error) == (
        "compile", "chinook", "done", None,
    )  # fmt: skip
    assert job.finished_at is not None
    assert step_states(got.job_id) == [
        (t, "skipped" if t == "PlaylistTrack" else "done", PACKS[t]) for t in ORDER
    ]
    [invoice] = [s for s in jobs.job_steps(got.job_id) if s.table_name == "Invoice"]
    assert invoice.detail == {"terms": ["Invoice"], "attributes": 8, "junction": False}
    [playlist_track] = [s for s in jobs.job_steps(got.job_id) if s.table_name == "PlaylistTrack"]
    assert playlist_track.detail == {"skipped": "no non-FK columns"}
    assert clock.sleeps == []
    # A table's checkpoint commits with its Terms: every table with a Term has a done step
    assert rows(
        store,
        "SELECT count(*) FROM table__term tt JOIN ops.job_step s ON s.table_id = tt.table_id "
        "WHERE s.state = 'done'",
    ) == ["10"]


def test_a_rate_limit_pauses_until_resets_at_then_resumes_in_the_same_run(
    store: pg.Layout, executor: SourceExecutor
) -> None:
    active = ActiveJobs()
    idle = [T0.timestamp()]
    lifetime = Lifetime(idle_timeout_s=60, clock=lambda: idle[0], jobs_running=active.count)
    during: list[Any] = []

    def look(seconds: float) -> None:
        job = jobs.open_job("compile", "chinook")
        assert job is not None
        idle[0] += seconds  # the whole pause passes with no client and no request
        during.append((job.state, job.paused_until, active.count(), lifetime.should_exit()))
        during.append(step_states(job.id))

    clock = FakeClock(on_sleep=look)
    gateway = ScriptedGateway(once_at_employee(NoAnswer("limited", "limited", RESETS_AT)))

    got = asyncio.run(compile_database("chinook", executor, gateway, jobs=active, clock=clock))

    assert clock.sleeps == [7200 + RESUME_MARGIN_S]
    # Paused until the reset, counted as running, so the engine stays up through the pause
    assert during[0] == ("paused", RESETS_AT, 1, False)
    # The small tables' pack and Customer are written; Employee and the rest are not
    assert [(t, s) for t, s, _ in during[1]] == [
        (t, "skipped" if t == "PlaylistTrack" else "done" if t in BEFORE_EMPLOYEE else "pending")
        for t in ORDER
    ]
    # Then Employee again, and the rest, in the same run
    assert got.complete and got.stopped is None
    assert [t.name for t in got.tables] == PACK_ORDER
    terms_calls = [table_of(c.user) for c in gateway.calls if c.task == "table_terms"]
    assert terms_calls.count("Employee") == 2
    job = jobs.job(got.job_id)
    assert (job.state, job.paused_until) == ("done", None)
    assert active.count() == 0
    idle[0] += 61
    assert lifetime.should_exit()
    fallbacks = "SELECT count(*) FROM term WHERE description LIKE 'Business entity%'"
    assert rows(store, fallbacks) == ["1"]  # Genre's, and none for Employee


def test_a_limit_without_resets_at_waits_a_minute(executor: SourceExecutor) -> None:
    clock = FakeClock()
    gateway = ScriptedGateway(once_at_employee(NoAnswer("limited", "ready")))  # an overload

    got = asyncio.run(compile_database("chinook", executor, gateway, clock=clock))

    assert clock.sleeps == [RETRY_WITHOUT_RESET_S + RESUME_MARGIN_S]
    assert got.complete


def test_a_logged_out_claude_stops_the_job_and_the_next_run_resumes_it(
    store: pg.Layout, executor: SourceExecutor
) -> None:
    clock = FakeClock()
    gateway = ScriptedGateway(once_at_employee(NoAnswer("error", "needs_login")))

    stopped = asyncio.run(compile_database("chinook", executor, gateway, clock=clock))

    assert clock.sleeps == []
    assert (stopped.complete, stopped.semantic_fks) == (False, None)
    assert stopped.stopped is not None and stopped.stopped.status.state == "needs_login"
    assert [t.name for t in stopped.tables] == PACK_ORDER[:8]
    job = jobs.job(stopped.job_id)
    assert job.state == "stopped" and job.error is not None and "needs_login" in job.error

    resumed = ScriptedGateway(claude)
    done = asyncio.run(compile_database("chinook", executor, resumed, clock=clock))

    assert done.job_id == stopped.job_id
    assert done.complete
    assert [t.name for t in done.tables] == PACK_ORDER[8:]
    assert table_of(resumed.calls[0].user) == "Employee"
    # The packs the job began with, though the work list is now eight tables
    assert step_states(done.job_id) == [
        (t, "skipped" if t == "PlaylistTrack" else "done", PACKS[t]) for t in ORDER
    ]
    assert jobs.job(done.job_id).state == "done"
    counts = "SELECT count(*) FROM term UNION ALL SELECT count(*) FROM column__semantic_fk"
    assert rows(store, counts) == ["10", "10"]


def test_a_person_pausing_stops_the_job_before_its_next_pack(
    store: pg.Layout, executor: SourceExecutor
) -> None:
    hold = asyncio.Event()

    def pause_in_the_small_pack(task: str, user: str) -> Any:
        if task == "table_terms" and "Artist" in tables_of(user):
            hold.set()
        return claude(task, user)

    gateway = ScriptedGateway(pause_in_the_small_pack)
    held = asyncio.run(compile_database("chinook", executor, gateway, hold=hold))

    # The small tables' pack was under way, so it finished; no later pack started
    assert held.held and not held.complete and held.stopped is None
    assert [t.name for t in held.tables] == SMALL
    asked = {t for c in gateway.calls for t in tables_of(c.user)}
    assert asked <= set(SMALL)
    job = jobs.job(held.job_id)
    assert (job.state, job.paused_until, job.error) == ("held", None, None)

    done = asyncio.run(compile_database("chinook", executor, ScriptedGateway(claude)))
    assert done.job_id == held.job_id and done.complete
    assert [t.name for t in done.tables] == PACK_ORDER[len(SMALL) :]
    assert jobs.job(done.job_id).state == "done"


def test_a_person_pausing_during_a_usage_pause_holds_the_job_at_once(
    executor: SourceExecutor,
) -> None:
    hold = asyncio.Event()
    clock = FakeClock(on_sleep=lambda _: hold.set())  # "Don't resume", while it waits
    gateway = ScriptedGateway(once_at_employee(NoAnswer("limited", "limited", RESETS_AT)))

    held = asyncio.run(compile_database("chinook", executor, gateway, clock=clock, hold=hold))

    assert held.held
    assert {t.name for t in held.tables} == BEFORE_EMPLOYEE
    job = jobs.job(held.job_id)
    assert (job.state, job.paused_until) == ("held", None)
    # The last call was Employee's that hit the limit: nothing was asked after it
    assert (gateway.calls[-1].task, table_of(gateway.calls[-1].user)) == ("table_terms", "Employee")


def test_each_packs_calls_are_made_under_its_job_and_pack(executor: SourceExecutor) -> None:
    scopes: list[tuple[CallScope, list[str]]] = []

    def note(task: str, user: str) -> Any:
        scopes.append((current_scope(), tables_of(user)))  # a description prompt names none
        return claude(task, user)

    got = asyncio.run(compile_database("chinook", executor, ScriptedGateway(note)))

    assert scopes and all(scope.job_id == got.job_id for scope, _ in scopes)
    for scope, asked in scopes:
        assert scope.pack is not None and scope.table_name is not None
        assert all(PACKS[t] == scope.pack for t in asked), (scope, asked)
        assert set(asked) <= set(scope.table_name.split(", "))
    assert {scope.pack for scope, _ in scopes} == set(PACKS.values())
    assert current_scope() == CallScope()


class Crash(Exception):
    """The engine dying in the middle of a call."""


def test_a_crash_leaves_the_job_running_and_the_next_run_resumes_it(
    executor: SourceExecutor,
) -> None:
    def crash_at_employee(task: str, user: str) -> Any:
        if task == "table_terms" and table_of(user) == "Employee":
            raise Crash
        return claude(task, user)

    with pytest.raises(Crash):
        asyncio.run(compile_database("chinook", executor, ScriptedGateway(crash_at_employee)))

    job = jobs.open_job("compile", "chinook")
    assert job is not None and job.state == "running"
    assert [t for t, s, _ in step_states(job.id) if s != "pending"] == [
        t for t in ORDER if t in BEFORE_EMPLOYEE
    ]

    resumed = ScriptedGateway(claude)
    done = asyncio.run(compile_database("chinook", executor, resumed))

    assert done.job_id == job.id and done.complete
    assert table_of(resumed.calls[0].user) == "Employee"
    asked = {table_of(c.user) for c in resumed.calls if c.task != "column_descriptions"}
    assert asked == set(PACK_ORDER[8:])


def test_a_job_paused_when_the_engine_stopped_waits_out_the_rest_of_the_pause(
    executor: SourceExecutor,
) -> None:
    def stop_engine(_: float) -> None:
        raise Crash

    gateway = ScriptedGateway(once_at_employee(NoAnswer("limited", "limited", RESETS_AT)))
    with pytest.raises(Crash):
        asyncio.run(
            compile_database("chinook", executor, gateway, clock=FakeClock(on_sleep=stop_engine))
        )
    paused = jobs.open_job("compile", "chinook")
    assert paused is not None and (paused.state, paused.paused_until) == ("paused", RESETS_AT)

    # The next engine starts an hour later; the reset is an hour off still
    clock = FakeClock(at=T0 + timedelta(hours=1))
    calls_at: list[int] = []  # how many sleeps came before each call

    def answer(task: str, user: str) -> Any:
        calls_at.append(len(clock.sleeps))
        return claude(task, user)

    resumed = ScriptedGateway(answer)
    done = asyncio.run(compile_database("chinook", executor, resumed, clock=clock))

    assert clock.sleeps == [3600 + RESUME_MARGIN_S]
    assert calls_at[0] == 1  # nothing was asked before the wait
    assert done.job_id == paused.id and done.complete
    assert table_of(resumed.calls[0].user) == "Employee"


def test_a_finished_job_is_not_resumed_and_a_new_one_plans_what_is_left(
    executor: SourceExecutor,
) -> None:
    first = asyncio.run(compile_database("chinook", executor, ScriptedGateway(claude)))

    again = asyncio.run(compile_database("chinook", executor, ScriptedGateway(claude)))

    assert again.job_id != first.job_id
    assert step_states(again.job_id) == [("PlaylistTrack", "skipped", 0)]
    assert jobs.open_job("compile", "chinook") is None
