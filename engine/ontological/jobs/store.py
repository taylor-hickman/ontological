"""ops.job and ops.job_step (ARCHITECTURE.md §5.2, §6.5): what a background job has done, so a
paused, stopped or crashed one resumes where it was.

A job is `running`, `paused` until a time, `held` by the person until they resume it, `stopped` by
an error a person has to fix (a logged-out `claude`, say), or `done`. A database has at most one
unfinished compile, and the next run resumes it. An `ingest` job is a staleness refresh, recorded
once it ends (`record_ingest`), `done` or `stopped` with its error, with the tables it found in
`result`; the Jobs screen offers to compile them until the person dismisses the prompt. A crashed
job is left `running`, which is why the engine counts the jobs it is running in memory
(`ActiveJobs`), never from this table.

Each step is one table, `pending` until it is `done` or `skipped`. Writes go through the DAL's
session, so `finish_step` inside a `write_transaction` commits with whatever else the block
writes: a compiled table's Terms and its checkpoint commit together or not at all. Every call
blocks; async callers run them in a thread.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from auto_ontology.dal.session import store, write_transaction
from ontological.compile.packing import PlannedStep

Kind = Literal["compile", "ingest"]
JobState = Literal["running", "paused", "held", "stopped", "done"]
StepState = Literal["pending", "done", "skipped"]


@dataclass(frozen=True)
class Job:
    id: int
    kind: Kind
    database_name: str
    state: JobState
    paused_until: datetime | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    result: dict[str, Any] | None = None  # an ingest's {"new_tables": [...]}
    dismissed_at: datetime | None = None  # when the person put off its new-tables prompt


@dataclass(frozen=True)
class Step:
    position: int
    table_id: str
    table_name: str
    pack: int
    state: StepState
    detail: dict[str, Any] | None
    finished_at: datetime | None


_JOB = (
    "SELECT id, kind, database_name, state, paused_until, error, created_at, finished_at, "
    "result, dismissed_at FROM ops.job"
)


def _job(row: dict[str, Any]) -> Job:
    return Job(**row)


def job(job_id: int) -> Job:
    [row] = store().query_read(f"{_JOB} WHERE id = :id", {"id": job_id})
    return _job(row)


def open_job(kind: Literal["compile"], database_name: str) -> Job | None:
    """The database's unfinished compile: the one to resume."""
    found = store().query_read(
        f"{_JOB} WHERE kind = :kind AND database_name = :db AND state <> 'done'",
        {"kind": kind, "db": database_name},
    )
    return _job(found[0]) if found else None


def unfinished(kind: Literal["compile"]) -> list[Job]:
    """Every unfinished job of this kind, oldest first."""
    rows = store().query_read(
        f"{_JOB} WHERE kind = :kind AND state <> 'done' ORDER BY id", {"kind": kind}
    )
    return [_job(row) for row in rows]


def recent(limit: int) -> list[Job]:
    """The latest jobs of every kind, newest first."""
    rows = store().query_read(f"{_JOB} ORDER BY id DESC LIMIT :limit", {"limit": limit})
    return [_job(row) for row in rows]


def record_ingest(
    database_name: str, started_at: datetime, new_tables: Sequence[str], error: str | None
) -> Job:
    """A staleness refresh that has ended: `done` with the tables it found, or `stopped` with why
    it failed."""
    with write_transaction():
        [row] = store().query_write(
            "INSERT INTO ops.job (kind, database_name, state, error, created_at, updated_at, "
            "finished_at, result) VALUES ('ingest', :db, :state, :error, :started, now(), now(), "
            "CAST(:result AS jsonb)) RETURNING id",
            {
                "db": database_name,
                "state": "stopped" if error else "done",
                "error": error,
                "started": started_at,
                "result": json.dumps({"new_tables": list(new_tables)}),
            },
        )
    return job(int(row["id"]))


def dismiss(job_id: int) -> None:
    """The person put off the job's new-tables prompt."""
    store().query_write(
        "UPDATE ops.job SET dismissed_at = now() WHERE id = :id AND dismissed_at IS NULL",
        {"id": job_id},
    )


def start_job(kind: Kind, database_name: str, steps: Sequence[PlannedStep]) -> Job:
    """A new running job with these steps, all pending."""
    with write_transaction():
        [row] = store().query_write(
            "INSERT INTO ops.job (kind, database_name, state) VALUES (:kind, :db, 'running') "
            "RETURNING id",
            {"kind": kind, "db": database_name},
        )
        job_id = int(row["id"])
        for step in steps:
            store().query_write(
                "INSERT INTO ops.job_step (job_id, position, table_id, table_name, pack) "
                "VALUES (:job, :position, :table_id, :name, :pack)",
                {
                    "job": job_id,
                    "position": step.position,
                    "table_id": step.table.id,
                    "name": step.table.name,
                    "pack": step.pack,
                },
            )
    return job(job_id)


def job_steps(job_id: int) -> list[Step]:
    rows = store().query_read(
        "SELECT position, table_id, table_name, pack, state, detail, finished_at "
        "FROM ops.job_step WHERE job_id = :job ORDER BY position",
        {"job": job_id},
    )
    return [Step(**row) for row in rows]


def finish_step(
    job_id: int, position: int, state: Literal["done", "skipped"], detail: dict[str, Any]
) -> None:
    store().query_write(
        "UPDATE ops.job_step SET state = :state, detail = CAST(:detail AS jsonb), "
        "finished_at = now() WHERE job_id = :job AND position = :position",
        {"job": job_id, "position": position, "state": state, "detail": json.dumps(detail)},
    )


def set_state(
    job_id: int,
    state: JobState,
    *,
    paused_until: datetime | None = None,
    error: str | None = None,
) -> None:
    """Move the job to `state`. `paused_until` goes with `paused`; `error` says why it stopped."""
    store().query_write(
        "UPDATE ops.job SET state = :state, paused_until = :until, error = :error, "
        "updated_at = now(), "
        "finished_at = CASE WHEN :state = 'done' THEN now() END WHERE id = :id",
        {"id": job_id, "state": state, "until": paused_until, "error": error},
    )
