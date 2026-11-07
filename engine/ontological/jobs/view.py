"""What the Jobs screen shows (DESIGN.md §5.4, BUILD_PLAN.md M3.8), read from ops.job, ops.job_step
and ops.llm_call.

- A job's summary: its state, its tables done of all, the Claude calls it spawned and the calls
  the cache answered, and for a refresh, the new tables it found.
- A compile's tables, each with the option it ran with and how many of that option's stages
  (`STAGES`) are done: split's four, or merged's one `table_semantics` call. A run can mix them,
  since a resumed or switched job takes the other for the packs left and merged leaves some
  tables to split, so each step records its own once its pack starts; a table whose pack hasn't
  started shows the job's. A table whose step is done has done every stage. One still pending
  has done the stages its pack's latest try got through, read from the calls logged under the
  pack (`stages_done`), and for merged under the table's own name: compile asks each split stage
  once for a pack's tables, and the gateway's cache keeps those answers, so a resumed pack gets
  them back without spending quota.
- A job's call log, newest first: time, task, table, model, and how it ended.

Every function blocks; async callers run them in a thread.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from auto_ontology.dal.session import store
from ontological.compile.merged import TASK as MERGED_TASK
from ontological.compile.packing import TableCalls
from ontological.compile.split import STAGES as SPLIT_STAGES
from ontological.jobs import store as job_store
from ontological.llm.calls import Outcome

JOBS_SHOWN = 20  # the current job and the earlier runs in the rail
CALLS_SHOWN = 50
# Each option's stages for a table, in the order their calls run: the tile's segments.
STAGES: dict[TableCalls, tuple[str, ...]] = {"split": SPLIT_STAGES, "merged": (MERGED_TASK,)}


class JobSummary(BaseModel):
    id: int
    kind: job_store.Kind
    database_name: str
    state: job_store.JobState
    paused_until: datetime | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    tables: int  # a compile's planned tables
    tables_done: int  # done or skipped
    calls: int  # spawns of `claude`
    cache_hits: int  # calls the cache answered
    new_tables: list[str]  # a refresh's new tables, `schema.table`
    dismissed: bool  # the person put off the new-tables prompt
    table_calls: TableCalls  # a compile's option for the tables it has left


class TableProgress(BaseModel):
    name: str
    state: job_store.StepState
    table_calls: TableCalls  # the option it ran with, or will
    stages_done: int  # of `JobDetail.stages[table_calls]`
    skipped: str | None  # why nothing was written, for a skipped table


class JobDetail(BaseModel):
    job: JobSummary
    stages: dict[TableCalls, list[str]]  # each option's stages, by route name, in order
    tables: list[TableProgress]


class CallRow(BaseModel):
    id: int
    at: datetime
    task: str
    table_name: str | None
    model: str  # the model that answered, or the route's when no answer said
    outcome: Outcome
    cached: bool
    attempt: int
    duration_ms: int | None


class StageCall(BaseModel):
    task: str
    outcome: Outcome
    attempt: int


_FAILED: frozenset[str] = frozenset({"limited", "error"})


def stages_done(calls: Sequence[StageCall], stages: Sequence[str] = SPLIT_STAGES) -> int:
    """How many of a pending table's stages, from the first, its latest try got through.

    A try ends where Claude didn't answer (`limited`, `error`); the job pauses or stops there and
    the next try runs the table from its first stage, served from the cache as far as the last one
    got. So while a try runs, count what it has done; when the latest call failed, count what the
    try it ended got through before it. A stage is through once a later stage has started, or its
    latest call answered: `ok`, or `invalid` on the retry, after which compile falls back."""
    order = {stage: i for i, stage in enumerate(stages)}
    calls = [c for c in calls if c.task in order]
    failures = [i for i, c in enumerate(calls) if c.outcome in _FAILED]
    failed_stage: int | None = None
    if failures and failures[-1] == len(calls) - 1:
        start = failures[-2] + 1 if len(failures) > 1 else 0
        attempt = calls[start : failures[-1]]
        failed_stage = order[calls[-1].task]
    else:
        attempt = calls[failures[-1] + 1 :] if failures else calls
    done = 0
    for i, stage in enumerate(stages):
        mine = [c for c in attempt if c.task == stage]
        if not mine or i == failed_stage:
            break
        later = any(order[c.task] > i for c in attempt)
        last = mine[-1]
        if later or last.outcome == "ok" or (last.outcome == "invalid" and last.attempt == 2):
            done += 1
        else:
            break
    return done


def summaries(limit: int = JOBS_SHOWN) -> list[JobSummary]:
    """The latest jobs, newest first."""
    jobs = job_store.recent(limit)
    return _summarize(jobs)


def summary(job_id: int) -> JobSummary | None:
    jobs = _find(job_id)
    return _summarize(jobs)[0] if jobs else None


def detail(job_id: int) -> JobDetail | None:
    """A job with its tables' progress; None when there is no such job."""
    found = summary(job_id)
    if found is None:
        return None
    steps = job_store.job_steps(job_id)
    pending = sorted({s.pack for s in steps if s.state == "pending"})
    calls: dict[int, list[tuple[str | None, StageCall]]] = {}
    if pending:
        rows = store().query_read(
            "SELECT pack, table_name, task, outcome, attempt FROM ops.llm_call "
            "WHERE job_id = :job AND pack = ANY(:packs) ORDER BY id",
            {"job": job_id, "packs": pending},
        )
        for row in rows:
            call = StageCall(task=row["task"], outcome=row["outcome"], attempt=row["attempt"])
            calls.setdefault(int(row["pack"]), []).append((row["table_name"], call))

    def progress(step: job_store.Step) -> TableProgress:
        option = step.table_calls or found.table_calls
        stages = STAGES[option]
        if step.state == "done":
            done = len(stages)
        else:
            mine = [
                call
                for table, call in calls.get(step.pack, [])
                if option == "split" or table == step.table_name
            ]
            done = stages_done(mine, stages)
        return TableProgress(
            name=step.table_name,
            state=step.state,
            table_calls=option,
            stages_done=done,
            skipped=(step.detail or {}).get("skipped") if step.state == "skipped" else None,
        )

    return JobDetail(
        job=found,
        stages={option: list(names) for option, names in STAGES.items()},
        tables=[progress(s) for s in steps],
    )


def calls(job_id: int, limit: int = CALLS_SHOWN) -> list[CallRow]:
    """The job's latest calls, newest first."""
    rows = store().query_read(
        "SELECT id, created_at AS at, task, table_name, "
        "coalesce(answered_model, resolved_model, model) AS model, outcome, cached, attempt, "
        "duration_ms FROM ops.llm_call WHERE job_id = :job ORDER BY id DESC LIMIT :limit",
        {"job": job_id, "limit": limit},
    )
    return [CallRow(**row) for row in rows]


def _find(job_id: int) -> list[job_store.Job]:
    try:
        return [job_store.job(job_id)]
    except ValueError:  # no such row: `job` unpacks an empty result
        return []


def _summarize(jobs: list[job_store.Job]) -> list[JobSummary]:
    if not jobs:
        return []
    ids = [j.id for j in jobs]
    steps = _by_job(
        "SELECT job_id, count(*) AS total, count(*) FILTER (WHERE state <> 'pending') AS done "
        "FROM ops.job_step WHERE job_id = ANY(:ids) GROUP BY job_id",
        ids,
    )
    spawned = _by_job(
        "SELECT job_id, count(*) FILTER (WHERE NOT cached) AS calls, "
        "count(*) FILTER (WHERE cached) AS hits "
        "FROM ops.llm_call WHERE job_id = ANY(:ids) GROUP BY job_id",
        ids,
    )
    return [
        JobSummary(
            id=j.id,
            kind=j.kind,
            database_name=j.database_name,
            state=j.state,
            paused_until=j.paused_until,
            error=j.error,
            created_at=j.created_at,
            finished_at=j.finished_at,
            tables=int(steps.get(j.id, {}).get("total", 0)),
            tables_done=int(steps.get(j.id, {}).get("done", 0)),
            calls=int(spawned.get(j.id, {}).get("calls", 0)),
            cache_hits=int(spawned.get(j.id, {}).get("hits", 0)),
            new_tables=list((j.result or {}).get("new_tables", [])),
            dismissed=j.dismissed_at is not None,
            table_calls=j.table_calls,
        )
        for j in jobs
    ]


def _by_job(query: str, ids: list[int]) -> dict[int, dict[str, Any]]:
    return {int(r["job_id"]): dict(r) for r in store().query_read(query, {"ids": ids})}
