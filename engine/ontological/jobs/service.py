"""The jobs the engine runs, and what the API asks of them (DESIGN.md §5.4, BUILD_PLAN.md M3.8).

`Jobs` runs compiles as tasks on the engine's event loop, one per database, each with the source
executor its catalog row names and the engine's one Claude gateway. A person can see a compile's
estimate first (§6.5), start or resume a compile with split or merged, switch an unfinished one's
option for the tables it has left, hold one (pause it until they resume it), and dismiss a
refresh's new-tables prompt.

When the engine starts, it resumes every compile an earlier engine left `running` (it crashed or
was stopped mid-table) or `paused` (it was waiting out a usage limit, and picks up by itself). A
held or stopped compile waits for the person. When the engine stops, it cancels its compiles
without changing their rows, so the next engine resumes them the same way.

The API holds a `JobsService`, so the engine process tests can serve the routes from a fake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select

from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store
from ontological.compile.estimate import Estimate, estimate_database
from ontological.compile.gateway import Structured
from ontological.compile.packing import TableCalls
from ontological.compile.split import Clock, compile_database
from ontological.embed.embedder import LocalEmbedder
from ontological.jobs import store as job_store
from ontological.jobs import view
from ontological.jobs.active import ActiveJobs, JobBusy
from ontological.sources.executor import SourceExecutor
from ontological.sources.spec import Source, from_record

log = logging.getLogger(__name__)

STOP_TIMEOUT_S = 10.0


class NoSuchJob(Exception):
    pass


class NoSource(Exception):
    """The store keeps no source for that database, so nothing can read it to compile."""


class NotNow(Exception):
    """The job's state doesn't allow that, such as holding a finished job."""


class JobsService(Protocol):
    async def summaries(self) -> list[view.JobSummary]: ...
    async def detail(self, job_id: int) -> view.JobDetail: ...
    async def calls(self, job_id: int) -> list[view.CallRow]: ...
    async def estimate(self, database_name: str) -> Estimate: ...
    async def compile(self, database_name: str, table_calls: TableCalls | None = None) -> None: ...
    async def choose(self, job_id: int, table_calls: TableCalls) -> None: ...
    async def hold(self, job_id: int) -> None: ...
    async def resume(self, job_id: int) -> None: ...
    async def dismiss(self, job_id: int) -> None: ...
    async def resume_unfinished(self) -> None: ...
    async def stop(self) -> None: ...


def source_of(database_name: str) -> Source | None:
    """The source a database was ingested from, as its catalog row keeps it (M2.6)."""
    db = s.catalog_database
    rows = store().query_read(
        select(db.c.connection)
        .where(db.c.name == database_name)
        .where(db.c.connection.is_not(None))
    )
    if not rows:
        return None
    try:
        return from_record(rows[0]["connection"])
    except ValueError:
        return None


@dataclass
class _Running:
    task: asyncio.Task[None]
    hold: asyncio.Event


class Jobs:
    def __init__(
        self,
        *,
        gateway: Structured,
        active: ActiveJobs,
        embedder: LocalEmbedder | None = None,
        find_source: Callable[[str], Source | None] = source_of,
        open_executor: Callable[[Source], SourceExecutor] = SourceExecutor,
        clock: Clock | None = None,
    ) -> None:
        self._gateway = gateway
        self._clock = clock
        self._active = active
        self._embedder = embedder
        self._find_source = find_source
        self._open = open_executor
        self._running: dict[str, _Running] = {}  # by database

    # --- reads ----------------------------------------------------------------------------------

    async def summaries(self) -> list[view.JobSummary]:
        return await asyncio.to_thread(view.summaries)

    async def detail(self, job_id: int) -> view.JobDetail:
        found = await asyncio.to_thread(view.detail, job_id)
        if found is None:
            raise NoSuchJob(job_id)
        return found

    async def calls(self, job_id: int) -> list[view.CallRow]:
        await self._job(job_id)
        return await asyncio.to_thread(view.calls, job_id)

    async def estimate(self, database_name: str) -> Estimate:
        """The calls, quota and time of the database's next compile, or the rest of its
        unfinished one, with split and with merged. Asks Claude nothing."""
        return await asyncio.to_thread(estimate_database, database_name)

    # --- actions --------------------------------------------------------------------------------

    async def compile(self, database_name: str, table_calls: TableCalls | None = None) -> None:
        """Start the database's compile, or resume its unfinished one, with `table_calls` for
        the tables it has left (None: a new one splits, a resumed one keeps its own). Uses
        Claude quota."""
        if database_name in self._running:
            raise JobBusy(f"a compile of {database_name} is already running")
        source = await asyncio.to_thread(self._find_source, database_name)
        if source is None:
            raise NoSource(database_name)
        hold = asyncio.Event()
        task = asyncio.create_task(
            self._run(source, hold, table_calls), name=f"compile {database_name}"
        )
        self._running[database_name] = _Running(task, hold)
        task.add_done_callback(lambda _: self._forget(database_name, task))

    async def choose(self, job_id: int, table_calls: TableCalls) -> None:
        """Switch an unfinished compile's option: a running one takes it from its next pack."""
        job = await self._job(job_id)
        if job.kind != "compile" or job.state == "done":
            raise NotNow(f"a {job.state} {job.kind} has no tables left")
        await asyncio.to_thread(job_store.set_table_calls, job_id, table_calls)

    async def hold(self, job_id: int) -> None:
        """Pause the compile until the person resumes it: before its next table, or at once while
        it waits out a usage limit."""
        job = await self._job(job_id)
        if job.kind != "compile" or job.state not in ("running", "paused"):
            raise NotNow(f"a {job.state} {job.kind} can't be paused")
        running = self._running.get(job.database_name)
        if running is not None:
            running.hold.set()
        else:  # left by an earlier engine and not running here
            await asyncio.to_thread(job_store.set_state, job_id, "held")

    async def resume(self, job_id: int) -> None:
        job = await self._job(job_id)
        if job.kind != "compile" or job.state == "done":
            raise NotNow(f"a {job.state} {job.kind} can't be resumed")
        await self.compile(job.database_name)

    async def dismiss(self, job_id: int) -> None:
        await self._job(job_id)
        await asyncio.to_thread(job_store.dismiss, job_id)

    # --- the engine's start and stop ------------------------------------------------------------

    async def resume_unfinished(self) -> None:
        for job in await asyncio.to_thread(job_store.unfinished, "compile"):
            if job.state not in ("running", "paused"):
                continue
            log.info("resuming compile job %d of %s (%s)", job.id, job.database_name, job.state)
            try:
                await self.compile(job.database_name)
            except (NoSource, JobBusy) as e:
                log.warning("can't resume compile job %d: %s", job.id, e)

    async def stop(self) -> None:
        """Cancel the running compiles, leaving their rows for the next engine to resume."""
        tasks = [r.task for r in self._running.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=STOP_TIMEOUT_S)

    # --- internals ------------------------------------------------------------------------------

    async def _job(self, job_id: int) -> job_store.Job:
        try:
            return await asyncio.to_thread(job_store.job, job_id)
        except ValueError:
            raise NoSuchJob(job_id) from None

    async def _run(
        self, source: Source, hold: asyncio.Event, table_calls: TableCalls | None
    ) -> None:
        name = source.database_name
        try:
            with self._open(source) as executor:
                await compile_database(
                    name,
                    executor,
                    self._gateway,
                    jobs=self._active,
                    embedder=self._embedder,
                    clock=self._clock,
                    hold=hold,
                    table_calls=table_calls,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A job that failed for a reason compile doesn't handle, such as a source that has
            # gone, mustn't look as if it is still running.
            log.exception("compile of %s failed", name)
            await asyncio.to_thread(_stop_open, name, f"{type(e).__name__}: {e}")

    def _forget(self, database_name: str, task: asyncio.Task[None]) -> None:
        running = self._running.get(database_name)
        if running is not None and running.task is task:
            del self._running[database_name]


def _stop_open(database_name: str, error: str) -> None:
    job = job_store.open_job("compile", database_name)
    if job is not None and job.state in ("running", "paused"):
        job_store.set_state(job.id, "stopped", error=error)
