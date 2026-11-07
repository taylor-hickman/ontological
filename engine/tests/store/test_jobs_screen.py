"""What the Jobs screen reads and does (DESIGN.md §5.4, BUILD_PLAN.md M3.8): a compile's counts, its
tables' stages and its call log from ops.job, ops.job_step and ops.llm_call, and the service that
starts, holds, resumes and dismisses jobs. Chinook in the bundled store, with a scripted Claude
that logs its calls the way the gateway does."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import NoAnswer, ScriptedGateway
from pydantic import BaseModel
from test_compile import ORDER, claude, table_of

from ontological.catalog.ingest import ingest
from ontological.compile.split import STAGES, compile_database
from ontological.jobs import store as jobs
from ontological.jobs import view
from ontological.jobs.active import ActiveJobs, JobBusy
from ontological.jobs.service import Jobs, NoSource, NotNow
from ontological.llm.calls import LlmCall, Outcome, PgCallLog, current_scope
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg


@dataclass
class LoggingGateway(ScriptedGateway):
    """The scripted Claude, logging each call to ops.llm_call under the caller's scope as the
    gateway does: a spawn per new question, and a cached row for one it has answered before."""

    log: PgCallLog | None = None
    answered: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])

    async def structured[M: BaseModel](
        self,
        task: str,
        system: str,
        user: str,
        schema: type[M],
        cache_key_extra: str = "",
        *,
        prompt_version: str = "",
    ) -> M | None:
        key = (task, user)
        cached = key in self.answered
        answer: M | None
        outcome: Outcome
        if cached:
            answer = schema.model_validate(self.answer(task, user))
            outcome = "ok"  # compile reads last_outcome only when there is no answer
        else:
            answer = await super().structured(
                task, system, user, schema, prompt_version=prompt_version
            )
            outcome = self.last_outcome or "error"
            if outcome == "ok":
                self.answered.add(key)
        scope = current_scope()
        assert self.log is not None
        self.log.record(
            LlmCall(
                task=task,
                kind="structured",
                prompt_version=prompt_version,
                cache_key=f"{task}:{hash(user)}",
                model="sonnet",
                effort="high",
                attempt=1,
                outcome=outcome,
                answered_model=None if cached else "claude-sonnet-5-5",
                duration_ms=None if cached else 1500,
                cached=cached,
                job_id=scope.job_id,
                pack=scope.pack,
                table_name=scope.table_name,
            )
        )
        return answer


@pytest.fixture
def call_log(store: pg.Layout) -> Any:
    log = PgCallLog(pg.url(store))
    yield log
    log.close()


@pytest.fixture
def source(store: pg.Layout, chinook: Path) -> SqliteSource:
    src = SqliteSource(chinook)
    with SourceExecutor(src) as ex:
        ingest(ex)
    return src


def limited_once_at(table: str) -> Callable[[str, str], Any]:
    """Claude, except that `table`'s first `table_terms` call hits the usage limit."""
    seen: list[bool] = []

    def answer(task: str, user: str) -> Any:
        if task == "table_terms" and table_of(user) == table and not seen:
            seen.append(True)
            return NoAnswer("limited", "limited", datetime.now(UTC) + timedelta(hours=1))
        return claude(task, user)

    return answer


@dataclass
class InstantClock:
    """Pauses pass at once; `on_sleep` sees what a paused job left behind."""

    on_sleep: Callable[[], None] = lambda: None

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        self.on_sleep()
        await asyncio.sleep(0)


class WaitingClock:
    """A pause that lasts until the test lets it end."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.sleeping = asyncio.Event()

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await self.release.wait()


# --- the view -----------------------------------------------------------------------------------


def test_a_compile_shows_its_tables_calls_and_cache_hits(
    source: SqliteSource, call_log: PgCallLog
) -> None:
    gateway = LoggingGateway(limited_once_at("Employee"), log=call_log)
    during: list[view.JobDetail] = []

    def look() -> None:
        job = jobs.open_job("compile", "chinook")
        assert job is not None
        found = view.detail(job.id)
        assert found is not None
        during.append(found)

    with SourceExecutor(source) as ex:
        got = asyncio.run(
            compile_database("chinook", ex, gateway, clock=InstantClock(on_sleep=look))
        )

    # While paused: the small tables' pack and Customer are done; Employee, a pack of its own, got
    # through keys and descriptions; Invoice's pack hasn't started
    [paused] = during
    assert paused.job.state == "paused" and paused.job.paused_until is not None
    assert paused.stages == list(STAGES)
    progress = {t.name: (t.state, t.stages_done) for t in paused.tables}
    assert progress["Album"] == ("done", 4)
    assert progress["Employee"] == ("pending", 2)
    assert progress["Invoice"] == ("pending", 0)
    assert (paused.job.tables, paused.job.tables_done) == (11, 8)

    [summary] = view.summaries()
    spawns = len(gateway.calls)  # every call that reached the scripted Claude
    hits = len([c for c in jobs_calls(got.job_id) if c.cached])
    assert summary.id == got.job_id and summary.state == "done"
    assert (summary.tables, summary.tables_done) == (11, 11)
    # Employee's retry asked keys and descriptions again, and the cache answered them
    assert hits >= 2
    assert (summary.calls, summary.cache_hits) == (spawns, hits)
    assert (summary.new_tables, summary.dismissed) == ([], False)

    done = view.detail(got.job_id)
    assert done is not None
    by_name = {t.name: t for t in done.tables}
    assert [t.name for t in done.tables] == ORDER
    assert (by_name["PlaylistTrack"].state, by_name["PlaylistTrack"].skipped) == (
        "skipped",
        "no non-FK columns",
    )
    assert all(t.stages_done == len(STAGES) for t in done.tables if t.state == "done")

    log = view.calls(got.job_id, limit=500)
    assert [c.id for c in log] == sorted((c.id for c in log), reverse=True), "newest first"
    limited = [c for c in log if c.outcome == "limited"]
    assert [(c.task, c.table_name, c.model) for c in limited] == [
        ("table_terms", "Employee", "claude-sonnet-5-5")
    ]
    cached = [c for c in log if c.cached]
    assert {c.table_name for c in cached} == {"Employee"}
    assert all(c.model == "sonnet" and c.duration_ms is None for c in cached)
    assert view.calls(got.job_id, limit=5) == log[:5]


def jobs_calls(job_id: int) -> list[view.CallRow]:
    return view.calls(job_id, limit=10_000)


def test_a_refresh_shows_its_new_tables_until_dismissed(store: pg.Layout) -> None:
    started = datetime.now(UTC)
    found = jobs.record_ingest("pagila", started, ["public.review", "public.badge"], None)
    failed = jobs.record_ingest("wwi", started, [], "OperationalError: server is down")

    newest, older = view.summaries()
    assert (newest.id, older.id) == (failed.id, found.id)
    assert (older.kind, older.state, older.new_tables) == (
        "ingest", "done", ["public.review", "public.badge"],
    )  # fmt: skip
    assert (newest.state, newest.error) == ("stopped", "OperationalError: server is down")
    assert (older.tables, older.calls) == (0, 0)

    jobs.dismiss(found.id)
    assert [s.dismissed for s in view.summaries()] == [False, True]
    assert view.detail(10_000) is None


# --- the service --------------------------------------------------------------------------------


async def until(check: Callable[[], bool], timeout_s: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.05)


def state(database_name: str = "chinook") -> str | None:
    found = [j for j in jobs.recent(5) if j.kind == "compile" and j.database_name == database_name]
    return found[0].state if found else None


def test_a_compile_started_by_the_person_runs_to_done(source: SqliteSource) -> None:
    active = ActiveJobs()

    async def scenario() -> None:
        service = Jobs(gateway=ScriptedGateway(claude), active=active)
        await service.compile("chinook")
        with pytest.raises(JobBusy):
            await service.compile("chinook")
        assert active.count() <= 1
        await until(lambda: state() == "done")
        await until(lambda: active.count() == 0)
        with pytest.raises(NoSource):
            await service.compile("nowhere")

    asyncio.run(scenario())
    [summary] = view.summaries()
    assert (summary.state, summary.tables_done) == ("done", 11)


def test_the_person_holds_a_paused_compile_and_resumes_it(source: SqliteSource) -> None:
    async def scenario() -> None:
        clock = WaitingClock()
        gateway = ScriptedGateway(limited_once_at("Employee"))
        service = Jobs(gateway=gateway, active=ActiveJobs(), clock=clock)
        await service.compile("chinook")
        await clock.sleeping.wait()
        [job] = [j for j in view.summaries() if j.kind == "compile"]
        assert job.state == "paused"

        await service.hold(job.id)  # "Don't resume"
        await until(lambda: state() == "held")
        with pytest.raises(NotNow):
            await service.hold(job.id)

        clock.release.set()  # a resumed job's pause has passed
        await service.resume(job.id)
        await until(lambda: state() == "done")
        with pytest.raises(NotNow):
            await service.resume(job.id)

    asyncio.run(scenario())


def test_a_compile_that_fails_for_another_reason_is_stopped_not_left_running(
    source: SqliteSource,
) -> None:
    def broken(task: str, user: str) -> Any:
        raise RuntimeError("the source moved")

    async def scenario() -> None:
        service = Jobs(gateway=ScriptedGateway(broken), active=ActiveJobs())
        await service.compile("chinook")
        await until(lambda: state() == "stopped")

    asyncio.run(scenario())
    [job] = view.summaries()
    assert job.error == "RuntimeError: the source moved"


def test_a_new_engine_resumes_running_and_paused_compiles_but_not_held_ones(
    store: pg.Layout, chinook: Path, tmp_path: Path
) -> None:
    for name in ("crashed", "waiting", "held"):
        path = tmp_path / f"{name}.sqlite"
        shutil.copy(chinook, path)
        with SourceExecutor(SqliteSource(path)) as ex:
            ingest(ex)
        job = jobs.start_job("compile", name, [])
        if name == "waiting":
            jobs.set_state(job.id, "paused", paused_until=datetime.now(UTC) - timedelta(minutes=1))
        if name == "held":
            jobs.set_state(job.id, "held")

    async def scenario() -> None:
        service = Jobs(gateway=ScriptedGateway(claude), active=ActiveJobs(), clock=InstantClock())
        await service.resume_unfinished()
        await until(lambda: state("crashed") == "done" and state("waiting") == "done")
        await service.stop()

    asyncio.run(scenario())
    assert state("held") == "held"


def test_stopping_the_engine_leaves_a_compile_for_the_next_one(source: SqliteSource) -> None:
    async def scenario() -> None:
        clock = WaitingClock()
        service = Jobs(
            gateway=ScriptedGateway(limited_once_at("Employee")), active=ActiveJobs(), clock=clock
        )
        await service.compile("chinook")
        await clock.sleeping.wait()
        await service.stop()

    asyncio.run(scenario())
    assert state() == "paused", "as it was, for the next engine to wait out and resume"
