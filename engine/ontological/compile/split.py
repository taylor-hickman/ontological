# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/compile.py, pipeline.py
# and visit_enter.py (`process_table`, `_commit_terms`) at 2a24f5d. Tables run one at a time,
# in upstream's canonical commit order, and so do the Claude calls. A table's terms and
# attributes are written in one transaction, with its checkpoint. The FK stage reads the table's
# columns after profiling has stored their samples, where upstream reads them before, so its
# prompt has samples on a first compile. Embedding, the SQL-attribute and bridge stages are
# later work.

"""The split compile (BUILD_PLAN.md M1.4, M3.1; ARCHITECTURE.md §1.3, §6.4, §6.5).

A compile is a job (ops.job) with a step per table (ops.job_step), planned when it starts from
the tables with no Term yet: canonical order, and the packs small tables share
(`compile.packing`). For each pending step, in order:

1. profile the table through the source executor, and store samples, uniqueness and date
   notations;
2. `table_keys`: suggest FKs and PKs and decide whether it is a junction table;
3. `column_descriptions`: describe its undescribed non-FK columns, 15 to a call;
4. `table_terms`: propose its Terms and assign every non-FK column to one;
5. write the Terms and their attributes, and mark the step done, in one transaction.

Then semantic FKs from the declared foreign keys. Every Claude call goes through the gateway,
one at a time. An answer that fails validation takes upstream's deterministic fallback. When
Claude doesn't answer at all, nothing is written for the table: on a rate limit or an overload
the job pauses until the usage window resets and retries the table; for anything else (a
logged-out or missing `claude`) it stops, and the next run resumes it. A crashed job is resumed
the same way. Packed calls, chunking wide tables and the `merged` option are M3.2's and later.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from auto_ontology.dal.attributes import merge_column_attribute
from auto_ontology.dal.datasources import (
    fetch_all_tables_without_term,
    fetch_table_by_id,
    fetch_table_context,
    mark_table_as_junction,
    store_column_date_formats,
    store_column_sample_values,
    store_column_uniqueness,
)
from auto_ontology.dal.session import store, write_transaction
from auto_ontology.dal.terms import upsert_table_term
from ontological.compile.descriptions import column_attribute_specs
from ontological.compile.gateway import ClaudeUnavailable, Structured
from ontological.compile.keys import fk_source_columns, suggest_keys
from ontological.compile.models import ColumnAttributeSpec, TermProposal
from ontological.compile.packing import PlannedTable, plan
from ontological.compile.semantic_fk import SemanticFks, resolve_declared_fks
from ontological.compile.terms import extract_terms
from ontological.jobs import store as job_store
from ontological.jobs.active import ActiveJobs
from ontological.profile.run import profile_table
from ontological.profile.sampling import TableProfile
from ontological.sources.executor import SourceExecutor

logger = logging.getLogger(__name__)

# A paused job resumes this long after `resets_at`, so a clock a little ahead of the server's
# doesn't retry before the window has reset.
RESUME_MARGIN_S = 5
# How long a limit with no `resets_at` (an overload, or a rate limit with no event) pauses for.
RETRY_WITHOUT_RESET_S = 60


@dataclass(frozen=True)
class CompiledTable:
    table_id: str
    name: str
    terms: list[str] = field(default_factory=list[str])  # as written, collisions qualified
    attributes: list[str] = field(default_factory=list[str])
    junction: bool = False
    skipped: str | None = None  # why nothing was written


@dataclass(frozen=True)
class Compiled:
    database_name: str
    job_id: int  # its ops.job row
    tables: list[CompiledTable]  # the tables this run wrote, or skipped, before any stop
    semantic_fks: SemanticFks | None  # None when the compile stopped
    stopped: ClaudeUnavailable | None = None  # why it stopped; the next run resumes the job

    @property
    def complete(self) -> bool:
        return self.stopped is None


class Clock(Protocol):
    """The wall clock a pause is timed by, and the wait itself."""

    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True)
class Checkpoint:
    """A table's step in its job."""

    job_id: int
    position: int

    def record(self, state: Literal["done", "skipped"], detail: dict[str, Any]) -> None:
        """Joins the caller's write transaction, when there is one."""
        job_store.finish_step(self.job_id, self.position, state, detail)


async def compile_database(
    database_name: str,
    executor: SourceExecutor,
    gateway: Structured,
    *,
    probe_distinct_values: bool = True,
    jobs: ActiveJobs | None = None,
    clock: Clock | None = None,
) -> Compiled:
    """Compile every table of `database_name` that has no Term yet, then its semantic FKs.
    `executor` holds the database's source.

    Resumes the database's unfinished compile job, on the plan it began with, or starts one.
    A job paused when an earlier engine stopped waits out the rest of its pause first. `jobs`
    counts the job until this returns, paused or not, so the engine stays up for it. When
    Claude can't answer for a reason a wait won't fix, returns what this run did, with
    `stopped` set."""
    clock = clock or SystemClock()
    with (jobs or ActiveJobs()).track("compile", database_name):
        job = await asyncio.to_thread(_open_or_start, database_name)
        if job.state == "paused" and job.paused_until is not None:
            logger.info("compile job %d is paused until %s", job.id, job.paused_until)
            await _wait_until(clock, job.paused_until)
        if job.state != "running":
            await asyncio.to_thread(job_store.set_state, job.id, "running")
        pending = await asyncio.to_thread(_pending, job.id, database_name)
        logger.info("compile job %d: %d table(s) of %s", job.id, len(pending), database_name)
        done: list[CompiledTable] = []
        for number, (step, table) in enumerate(pending, start=1):
            checkpoint = Checkpoint(job.id, step.position)
            if table is None:
                logger.warning("[%s] has left the work list; skipping", step.table_name)
                await asyncio.to_thread(
                    checkpoint.record, "skipped", {"skipped": "not in the work list"}
                )
                continue
            logger.info("[%d/%d] %s", number, len(pending), step.table_name)
            while True:
                try:
                    compiled = await compile_table(
                        table,
                        executor,
                        gateway,
                        probe_distinct_values=probe_distinct_values,
                        checkpoint=checkpoint,
                    )
                    break
                except ClaudeUnavailable as e:
                    if e.outcome != "limited":
                        logger.warning(
                            "compile job %d stopped at %s, %d table(s) left: %s",
                            job.id,
                            step.table_name,
                            len(pending) - number + 1,
                            e,
                        )
                        await asyncio.to_thread(
                            job_store.set_state, job.id, "stopped", error=str(e)
                        )
                        return Compiled(database_name, job.id, done, None, stopped=e)
                    await _pause(job.id, clock, e)
            done.append(compiled)
        fks = await asyncio.to_thread(resolve_declared_fks, database_name)
        await asyncio.to_thread(job_store.set_state, job.id, "done")
        return Compiled(database_name, job.id, done, fks)


async def _pause(job_id: int, clock: Clock, limited: ClaudeUnavailable) -> None:
    """Pause the job until the usage window resets (§6.5), then mark it running again."""
    until = limited.status.limited_until or clock.now() + timedelta(seconds=RETRY_WITHOUT_RESET_S)
    logger.warning("compile job %d paused until %s: %s", job_id, until.isoformat(), limited)
    await asyncio.to_thread(job_store.set_state, job_id, "paused", paused_until=until)
    await _wait_until(clock, until)
    await asyncio.to_thread(job_store.set_state, job_id, "running")


async def _wait_until(clock: Clock, until: datetime) -> None:
    seconds = max(0.0, (until - clock.now()).total_seconds()) + RESUME_MARGIN_S
    await clock.sleep(seconds)


def _open_or_start(database_name: str) -> job_store.Job:
    """The database's unfinished compile job, or a new one planned from its work list."""
    found = job_store.open_job("compile", database_name)
    if found is not None:
        logger.info("resuming compile job %d of %s (%s)", found.id, database_name, found.state)
        return found
    tables = fetch_all_tables_without_term(database_name)
    columns = _column_counts([str(t["id"]) for t in tables])
    planned = [
        PlannedTable(
            id=str(t["id"]),
            schema_name=str(t.get("schema_name") or ""),
            name=str(t["name"]),
            columns=columns.get(str(t["id"]), 0),
        )
        for t in tables
    ]
    return job_store.start_job("compile", database_name, plan(database_name, planned))


def _column_counts(table_ids: list[str]) -> dict[str, int]:
    if not table_ids:
        return {}
    rows = store().query_read(
        "SELECT table_id, count(*) AS n FROM catalog_column WHERE table_id = ANY(:ids) "
        "GROUP BY table_id",
        {"ids": table_ids},
    )
    return {str(r["table_id"]): int(r["n"]) for r in rows}


def _pending(job_id: int, database_name: str) -> list[tuple[job_store.Step, dict[str, Any] | None]]:
    """The job's pending steps in order, each with its table from the work list, or None for a
    table that has left it (one a re-ingest dropped, say)."""
    work = {str(t["id"]): t for t in fetch_all_tables_without_term(database_name)}
    return [
        (step, work.get(step.table_id))
        for step in job_store.job_steps(job_id)
        if step.state == "pending"
    ]


async def compile_table(
    table: dict[str, Any],
    executor: SourceExecutor,
    gateway: Structured,
    *,
    probe_distinct_values: bool = True,
    checkpoint: Checkpoint | None = None,
) -> CompiledTable:
    """Compile one table. With a `checkpoint`, its step is marked done or skipped in the
    transaction that writes the table's result."""
    table_id, name = str(table["id"]), str(table["name"])
    loaded = await asyncio.to_thread(_profile, table, executor, probe_distinct_values)
    if loaded is None:
        logger.warning("[%s] has no columns; skipping", name)
        await asyncio.to_thread(_skip, table_id, "no columns", False, checkpoint)
        return CompiledTable(table_id, name, skipped="no columns")
    table, ctx, profile = loaded
    profiling: dict[str, Any] = dict(profile["columns"])

    keys = await suggest_keys(gateway, table, ctx, profiling)
    fk_names = fk_source_columns(ctx.get("fks", [])) | {s.column_name for s in keys.suggestions}
    # Payload-bearing junctions are wider than the two-key shape the bridge pass handles, so
    # the model's decision needs structural evidence of two FK roles too.
    junction = len(ctx.get("columns", [])) > 2 and keys.is_junction_table and len(fk_names) >= 2

    specs = await column_attribute_specs(
        gateway,
        ctx.get("columns", []),
        ctx.get("fks", []),
        suggested_fk_columns=fk_names,
        profiling=profiling,
    )
    if not specs:
        logger.warning("[%s] has no non-FK columns; no Term", name)
        await asyncio.to_thread(_skip, table_id, "no non-FK columns", junction, checkpoint)
        return CompiledTable(table_id, name, junction=junction, skipped="no non-FK columns")

    terms = await extract_terms(gateway, table, specs)
    written, attrs = await asyncio.to_thread(_commit, table_id, terms, specs, junction, checkpoint)
    logger.info(
        "[%s] Terms %s, %d attributes, %d FK columns", name, written, len(attrs), len(fk_names)
    )
    return CompiledTable(table_id, name, written, attrs, junction)


def _profile(
    table: dict[str, Any], executor: SourceExecutor, probe_distinct_values: bool
) -> tuple[dict[str, Any], dict[str, Any], TableProfile] | None:
    """Profile the table and store what profiling found. Returns the table with its declared
    primary key, its context read after the store took the samples, and the profile."""
    table_id = table["id"]
    ctx = fetch_table_context(table_id)
    if not ctx.get("columns"):
        return None
    profile = profile_table(
        executor,
        table.get("schema_name"),
        table["name"],
        ctx["columns"],
        probe_distinct_values=probe_distinct_values,
    )
    with write_transaction():
        store_column_sample_values(table_id, profile["sample_values"])
        store_column_uniqueness(table_id, profile["uniqueness"])
        store_column_date_formats(table_id, profile["date_formats"])
    row = fetch_table_by_id(table_id) or {}
    return {**table, "pk": row.get("pk") or []}, fetch_table_context(table_id), profile


def _skip(table_id: str, reason: str, junction: bool, checkpoint: Checkpoint | None) -> None:
    with write_transaction():
        if junction:
            mark_table_as_junction(table_id)
        if checkpoint is not None:
            checkpoint.record("skipped", {"skipped": reason})


def _commit(
    table_id: str,
    terms: list[TermProposal],
    specs: list[ColumnAttributeSpec],
    junction: bool,
    checkpoint: Checkpoint | None,
) -> tuple[list[str], list[str]]:
    """Write the Terms and their attributes, and the checkpoint, in one transaction. A Term
    whose name another table holds is written under a qualified name."""
    spec_by_column = {spec.source_column: spec for spec in specs}
    written: list[str] = []
    attrs: list[str] = []
    with write_transaction():
        if junction:
            mark_table_as_junction(table_id)
        for term in terms:
            assignments = [a for a in term.attributes if a.source_column in spec_by_column]
            if not assignments:
                continue
            persisted = upsert_table_term(
                term.name, term.description, table_id, synonyms=term.synonyms
            )
            if persisted is None:
                continue
            _, term_name = persisted
            if term_name != term.name:
                logger.info("qualified colliding Term name %r as %r", term.name, term_name)
            written.append(term_name)
            for assignment in assignments:
                spec = spec_by_column[assignment.source_column]
                merge_column_attribute(
                    term_name=term_name,
                    table_id=table_id,
                    source_column=spec.source_column,
                    attr_name=spec.display_name,
                    datatype=spec.datatype,
                    description=spec.description,
                )
                attrs.append(spec.display_name)
        if checkpoint is not None:
            checkpoint.record(
                "done", {"terms": written, "attributes": len(attrs), "junction": junction}
            )
    return written, attrs
