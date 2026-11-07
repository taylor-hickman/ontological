"""A JobsService from a scenario file, for the app's end-to-end tests (apps/desktop/test/e2e).

<appData>/jobs.json, when it exists, holds what the Jobs screen should show:

    {"jobs": [JobSummary, ...], "details": {"<id>": {"tables": [...]}},
     "calls": {"<id>": [CallRow, ...]}, "estimates": {"<database>": Estimate}}

A table with no `table_calls` takes its job's. Actions change the jobs in memory, as the real
service would change their rows: hold makes a compile `held`, resume and compile make it
`running`, choose sets its option, dismiss puts off a refresh's prompt. Each action is appended to
<appData>/jobs.log, so a test can see what the app asked for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ontological.compile.estimate import Estimate, estimate
from ontological.compile.packing import TableCalls
from ontological.jobs.active import JobBusy
from ontological.jobs.service import NoSource, NoSuchJob, NotNow
from ontological.jobs.view import STAGES, CallRow, JobDetail, JobSummary, TableProgress


class FakeJobs:
    def __init__(self, app_data: Path) -> None:
        self._log = app_data / "jobs.log"
        path = app_data / "jobs.json"
        scenario: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
        self._jobs = [JobSummary.model_validate(j) for j in scenario.get("jobs", [])]
        self._details: dict[str, Any] = scenario.get("details", {})
        self._calls: dict[str, Any] = scenario.get("calls", {})
        self._estimates: dict[str, Any] = scenario.get("estimates", {})

    def _note(self, line: str) -> None:
        with open(self._log, "a") as f:
            f.write(line + "\n")

    def _find(self, job_id: int) -> JobSummary:
        for job in self._jobs:
            if job.id == job_id:
                return job
        raise NoSuchJob(job_id)

    def _set(self, job_id: int, **changes: Any) -> None:
        self._jobs = [j.model_copy(update=changes) if j.id == job_id else j for j in self._jobs]

    async def summaries(self) -> list[JobSummary]:
        return list(self._jobs)

    async def detail(self, job_id: int) -> JobDetail:
        job = self._find(job_id)
        found = self._details.get(str(job_id), {})
        tables = [
            TableProgress.model_validate({"table_calls": job.table_calls, **t})
            for t in found.get("tables", [])
        ]
        stages: dict[TableCalls, list[str]] = {o: list(names) for o, names in STAGES.items()}
        return JobDetail(job=job, stages=stages, tables=tables)

    async def calls(self, job_id: int) -> list[CallRow]:
        self._find(job_id)
        return [CallRow.model_validate(c) for c in self._calls.get(str(job_id), [])]

    async def estimate(self, database_name: str) -> Estimate:
        """A read, polled while the screen is open, so it isn't logged."""
        found = self._estimates.get(database_name)
        return Estimate.model_validate(found) if found else estimate(database_name, [])

    async def compile(self, database_name: str, table_calls: TableCalls | None = None) -> None:
        self._note(f"compile {database_name}" + (f" {table_calls}" if table_calls else ""))
        compiles = [
            j for j in self._jobs if j.kind == "compile" and j.database_name == database_name
        ]
        if not compiles:
            raise NoSource(database_name)
        if compiles[0].state == "running":
            raise JobBusy(f"a compile of {database_name} is already running")
        changes: dict[str, Any] = {"state": "running", "paused_until": None, "error": None}
        if table_calls:
            changes["table_calls"] = table_calls
        self._set(compiles[0].id, **changes)

    async def choose(self, job_id: int, table_calls: TableCalls) -> None:
        self._note(f"choose {job_id} {table_calls}")
        job = self._find(job_id)
        if job.kind != "compile" or job.state == "done":
            raise NotNow(job.state)
        self._set(job_id, table_calls=table_calls)

    async def hold(self, job_id: int) -> None:
        self._note(f"hold {job_id}")
        job = self._find(job_id)
        if job.state not in ("running", "paused"):
            raise NotNow(job.state)
        self._set(job_id, state="held", paused_until=None)

    async def resume(self, job_id: int) -> None:
        self._note(f"resume {job_id}")
        job = self._find(job_id)
        if job.state == "done":
            raise NotNow(job.state)
        self._set(job_id, state="running", paused_until=None, error=None)

    async def dismiss(self, job_id: int) -> None:
        self._note(f"dismiss {job_id}")
        self._find(job_id)
        self._set(job_id, dismissed=True)

    async def auto_compile(self, refresh_id: int) -> bool:
        return False  # the fake store finds no sources to refresh

    async def resume_unfinished(self) -> None:
        pass

    async def stop(self) -> None:
        pass
