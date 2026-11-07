# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/compile.py, pipeline.py
# and visit_enter.py (`process_table`, `_commit_terms`) at 2a24f5d. Packs run one at a time, in
# the order of their first tables, and so do the Claude calls; small tables share a pack's calls,
# where upstream asks each table on its own. A pack's Terms and attributes are written in one
# transaction, with its checkpoints. The FK stage reads the table's columns after profiling has
# stored their samples, where upstream reads them before, so its prompt has samples on a first
# compile. The Terms and attributes are embedded once the compile ends or stops, not as each
# table commits (ontological/embed/semantic.py). The post-stages that make Claude calls (SQL
# attributes, bridges, PII) pause and retry on a rate limit as the packs do.

"""The split compile (BUILD_PLAN.md M1.4, M3.1, M3.2; ARCHITECTURE.md §1.3, §6.4, §6.5).

A compile is a job (ops.job) with a step per table (ops.job_step), planned when it starts from
the tables with no Term yet: canonical order, and the packs small tables share
(`compile.packing`). For each pack with pending steps, in the order of the packs' first tables:

1. profile each table through the source executor, and store samples, uniqueness and date
   notations;
2. `table_keys`: suggest FKs and PKs and decide whether each table is a junction;
3. `column_descriptions`: describe the undescribed non-FK columns, 15 to a call;
4. `table_terms`: propose each table's Terms and assign every non-FK column to one, a table
   wider than 40 such columns in chunks of 40;
5. `table_synonyms`: the alternate names each table's description writes out, for its Terms;
6. write the Terms and their attributes, and mark the steps done, in one transaction.

Each stage asks once for the pack's tables that have something to ask (`compile.packed`); a
table that is alone in that, and a wide table, which is a pack of its own, gets upstream's prompt
unchanged. Then the post-stages, for the whole database, on every run:

7. semantic FKs from the declared foreign keys (`compile.semantic_fk`);
8. SQL attributes the judge names from query history (`compile.sql_attributes`);
9. a join-pattern SQL attribute for each bridge table (`compile.bridges`);
10. with `pii` on, PII tags on the columns no run has classified (`ontological.pii`), then, on
    or off, the tags carried to the attributes built from tagged columns.

And, given an embedder, the Terms' and attributes' vectors in the semantic layer
(`ontological.embed.semantic`), for retrieval.
Every Claude call goes through the gateway, one at a time. An answer that fails validation takes
upstream's deterministic fallback. When Claude doesn't answer at all, nothing is written for the
pack: on a rate limit or an overload the job pauses until the usage window resets and retries
the pack; for anything else (a logged-out or missing `claude`) it stops, still embedding the
tables it wrote, and the next run resumes it. A crashed job is resumed the same way, and a
resumed pack's calls are the ones its first try made, so the gateway's cache answers those that
were answered. A certified description is never overwritten (the DAL's writes guard it). The
`merged` option is M3.3's.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
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
from ontological.catalog.parse_pool import ParsePool
from ontological.compile.bridges import Bridges, build_bridge_sql_attributes
from ontological.compile.descriptions import DescriptionsRequest, column_attribute_specs_pack
from ontological.compile.gateway import ClaudeUnavailable, Structured
from ontological.compile.keys import KeysRequest, fk_source_columns, suggest_keys_pack
from ontological.compile.models import ColumnAttributeSpec, TermProposal
from ontological.compile.packing import PlannedTable, plan
from ontological.compile.semantic_fk import SemanticFks, resolve_declared_fks
from ontological.compile.sql_attributes import SqlAttributes, suggest_sql_attributes
from ontological.compile.synonyms import extract_synonyms_pack
from ontological.compile.terms import extract_terms_pack
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import semantic_vdb
from ontological.embed.semantic import Synced, sync_database
from ontological.jobs import store as job_store
from ontological.jobs.active import ActiveJobs
from ontological.pii.service import (
    PiiPropagation,
    PiiTagging,
    detect_and_tag_pii,
    propagate_pii_to_attributes,
)
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
    semantic_fks: SemanticFks | None  # None when the compile stopped before them
    stopped: ClaudeUnavailable | None = None  # why it stopped; the next run resumes the job
    vectors: Synced | None = None  # the semantic layer's sync; None without an embedder
    post: PostStages | None = None  # None when the compile stopped before them

    @property
    def complete(self) -> bool:
        return self.stopped is None


@dataclass(frozen=True)
class PostStages:
    """What the post-stages after semantic FKs did, each None when the compile stopped before
    it, or, for `pii`, when it is off."""

    sql_attributes: SqlAttributes | None = None
    bridges: Bridges | None = None
    pii: PiiTagging | None = None
    pii_propagation: PiiPropagation | None = None


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
    embedder: LocalEmbedder | None = None,
    pool: ParsePool | None = None,
    pii: bool = False,
) -> Compiled:
    """Compile every table of `database_name` that has no Term yet, then run the post-stages
    (semantic FKs, SQL attributes, bridges, and PII when `pii` is on), then, given an
    `embedder`, sync the database's vectors in the semantic layer. `executor` holds the
    database's source. History fragments are extracted in `pool`, or in a pool of its own.

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
        packs = _by_pack(pending)
        logger.info(
            "compile job %d: %d table(s) of %s in %d pack(s)",
            job.id,
            len(pending),
            database_name,
            len(packs),
        )
        done: list[CompiledTable] = []
        left = len(pending)
        for number, members in enumerate(packs, start=1):
            work: list[tuple[dict[str, Any], Checkpoint]] = []
            for step, table in members:
                checkpoint = Checkpoint(job.id, step.position)
                if table is None:
                    logger.warning("[%s] has left the work list; skipping", step.table_name)
                    await asyncio.to_thread(
                        checkpoint.record, "skipped", {"skipped": "not in the work list"}
                    )
                    continue
                work.append((table, checkpoint))
            left -= len(members) - len(work)
            if not work:
                continue
            names = ", ".join(str(table["name"]) for table, _ in work)
            logger.info("[pack %d/%d] %s", number, len(packs), names)
            while True:
                try:
                    compiled = await compile_pack(
                        work, executor, gateway, probe_distinct_values=probe_distinct_values
                    )
                    break
                except ClaudeUnavailable as e:
                    if e.outcome != "limited":
                        logger.warning(
                            "compile job %d stopped at %s, %d table(s) left: %s",
                            job.id,
                            names,
                            left,
                            e,
                        )
                        await asyncio.to_thread(
                            job_store.set_state, job.id, "stopped", error=str(e)
                        )
                        vectors = await _embed(database_name, embedder)
                        return Compiled(
                            database_name, job.id, done, None, stopped=e, vectors=vectors
                        )
                    await _pause(job.id, clock, e)
            done.extend(compiled)
            left -= len(work)
        fks = await asyncio.to_thread(resolve_declared_fks, database_name)
        post = PostStages()
        try:
            post = await _post_stages(job.id, database_name, gateway, clock, pool, pii, post)
        except _Stopped as s:
            logger.warning("compile job %d stopped in its post-stages: %s", job.id, s.reason)
            await asyncio.to_thread(job_store.set_state, job.id, "stopped", error=str(s.reason))
            vectors = await _embed(database_name, embedder)
            return Compiled(
                database_name, job.id, done, fks, stopped=s.reason, vectors=vectors, post=s.post
            )
        vectors = await _embed(database_name, embedder)
        await asyncio.to_thread(job_store.set_state, job.id, "done")
        return Compiled(database_name, job.id, done, fks, vectors=vectors, post=post)


class _Stopped(Exception):
    def __init__(self, reason: ClaudeUnavailable, post: PostStages) -> None:
        self.reason = reason
        self.post = post


async def _post_stages(
    job_id: int,
    database_name: str,
    gateway: Structured,
    clock: Clock,
    pool: ParsePool | None,
    pii: bool,
    post: PostStages,
) -> PostStages:
    """The post-stages that may ask Claude, in upstream's order. A rate limit pauses the job and
    reruns the stage, whose writes so far make the rerun skip them (and the cache answers what
    was asked). Raises `_Stopped`, with what finished, when Claude can't answer otherwise."""

    async def answered[T](run: Callable[[], Awaitable[T]]) -> T:
        while True:
            try:
                return await run()
            except ClaudeUnavailable as e:
                if e.outcome != "limited":
                    raise _Stopped(e, post) from e
                await _pause(job_id, clock, e)

    post = replace(
        post,
        sql_attributes=await answered(
            lambda: suggest_sql_attributes(database_name, gateway, pool=pool)
        ),
    )
    post = replace(
        post, bridges=await answered(lambda: build_bridge_sql_attributes(database_name, gateway))
    )
    if pii:
        post = replace(post, pii=await answered(lambda: detect_and_tag_pii(database_name, gateway)))
    return replace(post, pii_propagation=await asyncio.to_thread(propagate_pii_to_attributes))


async def _embed(database_name: str, embedder: LocalEmbedder | None) -> Synced | None:
    if embedder is None:
        return None

    def sync() -> Synced:
        vdb = semantic_vdb(embedder)
        try:
            return sync_database(vdb, database_name)
        finally:
            vdb.close()

    return await asyncio.to_thread(sync)


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


def _by_pack(
    pending: list[tuple[job_store.Step, dict[str, Any] | None]],
) -> list[list[tuple[job_store.Step, dict[str, Any] | None]]]:
    """The pending steps by pack, in the order of the packs' numbers, which is the order of
    their first tables; each pack's steps in canonical order."""
    packs: dict[int, list[tuple[job_store.Step, dict[str, Any] | None]]] = {}
    for item in pending:
        packs.setdefault(item[0].pack, []).append(item)
    return [packs[number] for number in sorted(packs)]


@dataclass
class _Table:
    """A table of a pack on its way through the stages."""

    table: dict[str, Any]  # with its declared primary key, once profiled
    checkpoint: Checkpoint | None
    ctx: dict[str, Any] = field(default_factory=dict[str, Any])
    profiling: dict[str, Any] = field(default_factory=dict[str, Any])
    skipped: str | None = None
    junction: bool = False
    specs: list[ColumnAttributeSpec] = field(default_factory=list[ColumnAttributeSpec])
    terms: list[TermProposal] = field(default_factory=list[TermProposal])

    @property
    def id(self) -> str:
        return str(self.table["id"])

    @property
    def name(self) -> str:
        return str(self.table["name"])


async def compile_pack(
    tables: Sequence[tuple[dict[str, Any], Checkpoint | None]],
    executor: SourceExecutor,
    gateway: Structured,
    *,
    probe_distinct_values: bool = True,
) -> list[CompiledTable]:
    """Compile a pack's tables: profile each, then run each stage once for the pack (packed
    calls, `compile.packed`), then write every table, and mark each `checkpoint` done or
    skipped, in one transaction. Raises `ClaudeUnavailable`, having written nothing, when
    Claude doesn't answer."""
    work = [_Table(table, checkpoint) for table, checkpoint in tables]
    for t in work:
        loaded = await asyncio.to_thread(_profile, t.table, executor, probe_distinct_values)
        if loaded is None:
            logger.warning("[%s] has no columns; skipping", t.name)
            t.skipped = "no columns"
            continue
        t.table, t.ctx, profile = loaded
        t.profiling = dict(profile["columns"])

    live = [t for t in work if t.skipped is None]
    found = await suggest_keys_pack(
        gateway, [KeysRequest(t.table, t.ctx, t.profiling) for t in live]
    )
    fk_names: list[set[str]] = []
    for t, keys in zip(live, found, strict=True):
        names = fk_source_columns(t.ctx.get("fks", [])) | {s.column_name for s in keys.suggestions}
        # Payload-bearing junctions are wider than the two-key shape the bridge pass handles, so
        # the model's decision needs structural evidence of two FK roles too.
        t.junction = (
            len(t.ctx.get("columns", [])) > 2 and keys.is_junction_table and len(names) >= 2
        )
        fk_names.append(names)

    specs = await column_attribute_specs_pack(
        gateway,
        [
            DescriptionsRequest(
                t.name, t.ctx.get("columns", []), t.ctx.get("fks", []), names, t.profiling
            )
            for t, names in zip(live, fk_names, strict=True)
        ],
    )
    for t, table_specs in zip(live, specs, strict=True):
        t.specs = table_specs
        if not table_specs:
            logger.warning("[%s] has no non-FK columns; no Term", t.name)
            t.skipped = "no non-FK columns"

    named = [t for t in live if t.skipped is None]
    proposed = await extract_terms_pack(gateway, [(t.table, t.specs) for t in named])
    synonyms = await extract_synonyms_pack(
        gateway, [(t.name, t.table.get("description")) for t in named]
    )
    for t, terms, words in zip(named, proposed, synonyms, strict=True):
        t.terms = terms
        if words:
            for term in terms:
                term.synonyms = words

    return await asyncio.to_thread(_commit_pack, work)


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


def _commit_pack(work: list[_Table]) -> list[CompiledTable]:
    """Write the pack's tables, each with its checkpoint, in one transaction, so a pack is
    written whole or not at all and a resumed job asks the same packed calls again."""
    out: list[CompiledTable] = []
    with write_transaction():
        for t in work:
            if t.junction:
                mark_table_as_junction(t.id)
            if t.skipped is not None:
                if t.checkpoint is not None:
                    t.checkpoint.record("skipped", {"skipped": t.skipped})
                out.append(CompiledTable(t.id, t.name, junction=t.junction, skipped=t.skipped))
                continue
            written, attrs = _commit(t.id, t.terms, t.specs)
            if t.checkpoint is not None:
                t.checkpoint.record(
                    "done", {"terms": written, "attributes": len(attrs), "junction": t.junction}
                )
            logger.info("[%s] Terms %s, %d attributes", t.name, written, len(attrs))
            out.append(CompiledTable(t.id, t.name, written, attrs, t.junction))
    return out


def _commit(
    table_id: str, terms: list[TermProposal], specs: list[ColumnAttributeSpec]
) -> tuple[list[str], list[str]]:
    """Write the Terms and their attributes. A Term whose name another table holds is written
    under a qualified name."""
    spec_by_column = {spec.source_column: spec for spec in specs}
    written: list[str] = []
    attrs: list[str] = []
    for term in terms:
        assignments = [a for a in term.attributes if a.source_column in spec_by_column]
        if not assignments:
            continue
        persisted = upsert_table_term(term.name, term.description, table_id, synonyms=term.synonyms)
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
    return written, attrs
