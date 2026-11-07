"""A JobsService from a scenario file, for the app's end-to-end tests (apps/desktop/test/e2e).

<appData>/jobs.json, when it exists, holds what the Jobs screen should show:

    {"jobs": [JobSummary, ...], "details": {"<id>": {"stages": [...], "tables": [...]}},
     "calls": {"<id>": [CallRow, ...]}}

Actions change the jobs in memory, as the real service would change their rows: hold makes a
compile `held`, resume and compile make it `running`, dismiss puts off a refresh's prompt. Each
action is appended to <appData>/jobs.log, so a test can see what the app asked for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ontological.jobs.active import JobBusy
from ontological.jobs.service import NoSource, NoSuchJob, NotNow
from ontological.jobs.view import CallRow, JobDetail, JobSummary, TableProgress


class FakeJobs:
    def __init__(self, app_data: Path) -> None:
        self._log = app_data / "jobs.log"
        path = app_data / "jobs.json"
        scenario: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
        self._jobs = [JobSummary.model_validate(j) for j in scenario.get("jobs", [])]
        self._details: dict[str, Any] = scenario.get("details", {})
        self._calls: dict[str, Any] = scenario.get("calls", {})

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
        tables = [TableProgress.model_validate(t) for t in found.get("tables", [])]
        return JobDetail(job=job, stages=found.get("stages", []), tables=tables)

    async def calls(self, job_id: int) -> list[CallRow]:
        self._find(job_id)
        return [CallRow.model_validate(c) for c in self._calls.get(str(job_id), [])]

    async def compile(self, database_name: str) -> None:
        self._note(f"compile {database_name}")
        compiles = [
            j for j in self._jobs if j.kind == "compile" and j.database_name == database_name
        ]
        if not compiles:
            raise NoSource(database_name)
        if compiles[0].state == "running":
            raise JobBusy(f"a compile of {database_name} is already running")
        self._set(compiles[0].id, state="running", paused_until=None, error=None)

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
