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
# table commits (ontological/embed/semantic.py), and before the semantic FKs, whose candidates
# are searched from those vectors; again after the post-stages, for their SQL attributes. The
# post-stages that make Claude calls (semantic FKs, SQL attributes, bridges, PII) pause and retry
# on a rate limit as the packs do.

"""The compile (BUILD_PLAN.md M1.4, M3.1, M3.2, M3.3; ARCHITECTURE.md §1.3, §6.4, §6.5).

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
unchanged. With `table_calls="merged"`, steps 2 to 5 are one `table_semantics` call per table
instead (`compile.merged`), cleaned up as each stage cleans up its own; a table merged doesn't
suit, or whose answer is invalid, takes the split stages.

Then, given an embedder, the Terms' and attributes' vectors in the semantic layer
(`ontological.embed.semantic`), for retrieval, and the post-stages, for the whole database:

7. semantic FKs (`compile.semantic_fk`): the declared foreign keys, and, given the embedder and
   a job that planned tables, the rest inferred from those vectors 25 columns to a call and
   checked against the source's values through the executor's `probe` op;
8. SQL attributes the judge names from query history (`compile.sql_attributes`);
9. a join-pattern SQL attribute for each bridge table (`compile.bridges`);
10. with `pii` on, PII tags on the columns no run has classified (`ontological.pii`), then, on
    or off, the tags carried to the attributes built from tagged columns.

Then the vectors again, for what the post-stages wrote.
Every Claude call goes through the gateway, one at a time. An answer that fails validation takes
upstream's deterministic fallback. When Claude doesn't answer at all, nothing is written for the
pack, or no inferred FK: on a rate limit or an overload the job pauses until the usage window
resets and retries the pack, or the FK stage; for anything else (a logged-out or missing
`claude`) it stops, still embedding the tables it wrote, and the next run resumes it. A crashed
job is resumed the same way, and a resumed pack's calls are the ones its first try made, as are
the FK stage's, so the gateway's cache answers those that were answered. A certified description
is never overwritten (the DAL's writes guard it).

A person can pause the job too (`hold`): it stops before its next pack, or at once while it waits
out a usage limit, and stays `held` until they resume it. Each pack's calls are logged under the
job and the pack (`llm.calls.call_scope`), which is how the Jobs screen shows a table's stages.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import functools
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
from ontological.compile.merged import table_semantics
from ontological.compile.models import ColumnAttributeSpec, TableKeys, TermProposal
from ontological.compile.packing import PlannedTable, plan
from ontological.compile.semantic_fk import SemanticFks, resolve_semantic_fks
from ontological.compile.sql_attributes import SqlAttributes, suggest_sql_attributes
from ontological.compile.synonyms import extract_synonyms_pack
from ontological.compile.terms import extract_terms_pack
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import dense_search, semantic_vdb
from ontological.embed.semantic import Synced, sync_database
from ontological.jobs import store as job_store
from ontological.jobs.active import ActiveJobs
from ontological.llm.calls import call_scope
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
# A table's stages, in the order their calls run: the Jobs screen's segments (DESIGN.md §4.8).
STAGES = ("table_keys", "column_descriptions", "table_terms", "table_synonyms")
# What the semantic-FK stage's calls are logged for: the database, not a pack.
FK_SCOPE = "semantic FKs"

# The `compile.table_calls` setting (ARCHITECTURE.md §6.4).
type TableCalls = Literal["split", "merged"]


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
    held: bool = False  # the person paused it; resuming runs the rest
    post: PostStages | None = None  # None when the compile stopped or was held before them

    @property
    def complete(self) -> bool:
        return self.stopped is None and not self.held


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
    hold: asyncio.Event | None = None,
    pool: ParsePool | None = None,
    pii: bool = False,
    table_calls: TableCalls = "split",
) -> Compiled:
    """Compile every table of `database_name` that has no Term yet, then run the post-stages
    (semantic FKs, SQL attributes, bridges, and PII when `pii` is on), then, given an
    `embedder`, sync the database's vectors in the semantic layer. `executor` holds the
    database's source. History fragments are extracted in `pool`, or in a pool of its own.
    `table_calls` is the `compile.table_calls` setting (§6.4): `"split"` runs the four stages,
    `"merged"` one `table_semantics` call a table. It applies to the tables this run compiles,
    so a resumed job may take the other.

    Resumes the database's unfinished compile job, on the plan it began with, or starts one.
    A job paused when an earlier engine stopped waits out the rest of its pause first. `jobs`
    counts the job until this returns, paused or not, so the engine stays up for it. When
    Claude can't answer for a reason a wait won't fix, returns what this run did, with
    `stopped` set. Once `hold` is set, it stops before the next pack, or ends a pause early, and
    leaves the job `held`."""
    clock = clock or SystemClock()
    hold = hold or asyncio.Event()
    with (jobs or ActiveJobs()).track("compile", database_name):
        job = await asyncio.to_thread(_open_or_start, database_name)
        if job.state == "paused" and job.paused_until is not None:
            logger.info("compile job %d is paused until %s", job.id, job.paused_until)
            await _wait_until(clock, job.paused_until, hold)
        if hold.is_set():
            return await _held(job.id, database_name, [], embedder)
        if job.state != "running":
            await asyncio.to_thread(job_store.set_state, job.id, "running")
        pending = await asyncio.to_thread(_pending, job.id, database_name)
        packs = _by_pack(pending)
        logger.info(
            "compile job %d: %d table(s) of %s in %d pack(s), %s",
            job.id,
            len(pending),
            database_name,
            len(packs),
            table_calls,
        )
        done: list[CompiledTable] = []
        left = len(pending)
        for number, members in enumerate(packs, start=1):
            if hold.is_set():
                return await _held(job.id, database_name, done, embedder)
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
            with call_scope(job.id, members[0][0].pack, names):
                compiled = await _answered(
                    job.id,
                    clock,
                    hold,
                    functools.partial(
                        compile_pack,
                        work,
                        executor,
                        gateway,
                        probe_distinct_values=probe_distinct_values,
                        table_calls=table_calls,
                    ),
                )
            if isinstance(compiled, ClaudeUnavailable):
                logger.warning(
                    "compile job %d stopped at %s, %d table(s) left: %s",
                    job.id,
                    names,
                    left,
                    compiled,
                )
                return await _stopped(job.id, database_name, done, compiled, embedder)
            if compiled is None:
                return await _held(job.id, database_name, done, embedder)
            done.extend(compiled)
            left -= len(work)
        if hold.is_set():
            return await _held(job.id, database_name, done, embedder)
        # The FK stage searches the attributes' vectors, so the layer is synced first. It infers
        # FKs only for a job that planned tables: a column it passed on, or whose edge it pruned,
        # would otherwise be asked again by every compile, in a batch the cache doesn't know.
        infer = bool(await asyncio.to_thread(job_store.job_steps, job.id))
        synced = await _embed(database_name, embedder)
        with call_scope(job.id, None, FK_SCOPE):
            fks = await _answered(
                job.id,
                clock,
                hold,
                lambda: _semantic_fks(database_name, executor, gateway, embedder, infer),
            )
        if isinstance(fks, ClaudeUnavailable):
            logger.warning("compile job %d stopped at its semantic FKs: %s", job.id, fks)
            return await _stopped(job.id, database_name, done, fks, embedder, synced)
        if fks is None:
            return await _held(job.id, database_name, done, embedder, synced)
        post = PostStages()
        try:
            post = await _post_stages(job.id, database_name, gateway, clock, hold, pool, pii, post)
        except _Held as h:
            held = await _held(job.id, database_name, done, embedder, synced)
            return replace(held, post=h.post)
        except _Stopped as s:
            logger.warning("compile job %d stopped in its post-stages: %s", job.id, s.reason)
            await asyncio.to_thread(job_store.set_state, job.id, "stopped", error=str(s.reason))
            vectors = await _embed(database_name, embedder, synced)
            return Compiled(
                database_name, job.id, done, fks, stopped=s.reason, vectors=vectors, post=s.post
            )
        vectors = await _embed(database_name, embedder, synced)
        await asyncio.to_thread(job_store.set_state, job.id, "done")
        return Compiled(database_name, job.id, done, fks, vectors=vectors, post=post)


class _Stopped(Exception):
    def __init__(self, reason: ClaudeUnavailable, post: PostStages) -> None:
        self.reason = reason
        self.post = post


class _Held(Exception):
    def __init__(self, post: PostStages) -> None:
        self.post = post


async def _post_stages(
    job_id: int,
    database_name: str,
    gateway: Structured,
    clock: Clock,
    hold: asyncio.Event,
    pool: ParsePool | None,
    pii: bool,
    post: PostStages,
) -> PostStages:
    """The post-stages that may ask Claude, in upstream's order, their calls logged under the job
    and the stage's name. A rate limit pauses the job and reruns the stage, whose writes so far
    make the rerun skip them (and the cache answers what was asked). Raises `_Stopped`, with
    what finished, when Claude can't answer otherwise, and `_Held` when the person pauses the
    job, before a stage or during a pause; resuming runs the post-stages again."""

    async def answered[T](stage: str, run: Callable[[], Awaitable[T]]) -> T:
        while True:
            if hold.is_set():
                raise _Held(post)
            try:
                with call_scope(job_id, None, stage):
                    return await run()
            except ClaudeUnavailable as e:
                if e.outcome != "limited":
                    raise _Stopped(e, post) from e
                await _pause(job_id, clock, e, hold)

    post = replace(
        post,
        sql_attributes=await answered(
            "SQL attributes", lambda: suggest_sql_attributes(database_name, gateway, pool=pool)
        ),
    )
    post = replace(
        post,
        bridges=await answered(
            "bridge tables", lambda: build_bridge_sql_attributes(database_name, gateway)
        ),
    )
    if pii:
        post = replace(
            post, pii=await answered("PII", lambda: detect_and_tag_pii(database_name, gateway))
        )
    return replace(post, pii_propagation=await asyncio.to_thread(propagate_pii_to_attributes))


async def _answered[T](
    job_id: int, clock: Clock, hold: asyncio.Event, run: Callable[[], Awaitable[T]]
) -> T | ClaudeUnavailable | None:
    """`run()`'s result, paused and retried while Claude is over a usage limit (§6.5). Claude's
    error when it can't answer for a reason a wait won't fix; None when the person paused the
    job meanwhile."""
    while True:
        try:
            return await run()
        except ClaudeUnavailable as e:
            if e.outcome != "limited":
                return e
            await _pause(job_id, clock, e, hold)
            if hold.is_set():
                return None


async def _semantic_fks(
    database_name: str,
    executor: SourceExecutor,
    gateway: Structured,
    embedder: LocalEmbedder | None,
    infer: bool,
) -> SemanticFks:
    if embedder is None or not infer:
        return await resolve_semantic_fks(database_name, executor, gateway, None)
    vdb = semantic_vdb(embedder)
    try:
        return await resolve_semantic_fks(
            database_name,
            executor,
            gateway,
            lambda query, k, where: dense_search(vdb, query, k=k, where=where),
        )
    finally:
        vdb.close()


async def _stopped(
    job_id: int,
    database_name: str,
    done: list[CompiledTable],
    error: ClaudeUnavailable,
    embedder: LocalEmbedder | None,
    synced: Synced | None = None,
) -> Compiled:
    """Claude can't answer: leave the job `stopped`, with the tables it wrote embedded, for the
    next run to resume."""
    await asyncio.to_thread(job_store.set_state, job_id, "stopped", error=str(error))
    vectors = await _embed(database_name, embedder, synced)
    return Compiled(database_name, job_id, done, None, stopped=error, vectors=vectors)


async def _embed(
    database_name: str, embedder: LocalEmbedder | None, synced: Synced | None = None
) -> Synced | None:
    """Sync the database's vectors. Given the compile's earlier sync, `synced`, returns the two
    as one, so a compile reports one sync whichever stage it ended at."""
    if embedder is None:
        return None

    def sync() -> Synced:
        vdb = semantic_vdb(embedder)
        try:
            return sync_database(vdb, database_name)
        finally:
            vdb.close()

    later = await asyncio.to_thread(sync)
    return later if synced is None else synced.then(later)


async def _held(
    job_id: int,
    database_name: str,
    done: list[CompiledTable],
    embedder: LocalEmbedder | None,
    synced: Synced | None = None,
) -> Compiled:
    """The person paused the job: leave it `held`, with the tables it wrote embedded."""
    logger.info("compile job %d paused by the person", job_id)
    await asyncio.to_thread(job_store.set_state, job_id, "held")
    vectors = await _embed(database_name, embedder, synced)
    return Compiled(database_name, job_id, done, None, vectors=vectors, held=True)


async def _pause(
    job_id: int, clock: Clock, limited: ClaudeUnavailable, hold: asyncio.Event
) -> None:
    """Pause the job until the usage window resets (§6.5), then mark it running again, unless the
    person paused it meanwhile."""
    until = limited.status.limited_until or clock.now() + timedelta(seconds=RETRY_WITHOUT_RESET_S)
    logger.warning("compile job %d paused until %s: %s", job_id, until.isoformat(), limited)
    await asyncio.to_thread(job_store.set_state, job_id, "paused", paused_until=until)
    await _wait_until(clock, until, hold)
    if not hold.is_set():
        await asyncio.to_thread(job_store.set_state, job_id, "running")


async def _wait_until(clock: Clock, until: datetime, hold: asyncio.Event) -> None:
    """Sleep until `until`, plus the margin, or until `hold` is set."""
    seconds = max(0.0, (until - clock.now()).total_seconds()) + RESUME_MARGIN_S
    sleep = asyncio.ensure_future(clock.sleep(seconds))
    held = asyncio.ensure_future(hold.wait())
    try:
        finished, _ = await asyncio.wait({sleep, held}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        sleep.cancel()
        held.cancel()
    if sleep in finished:
        sleep.result()  # raises what the sleep raised


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
    table_calls: TableCalls = "split",
) -> list[CompiledTable]:
    """Compile a pack's tables: profile each, then run each split stage once for the pack
    (packed calls, `compile.packed`), or, with `table_calls="merged"`, ask each table's
    `table_semantics` (`compile.merged`), then write every table, and mark each `checkpoint`
    done or skipped, in one transaction. Raises `ClaudeUnavailable`, having written nothing, when
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
    if table_calls == "merged":
        live = await _merged_stage(gateway, live)
    await _split_stages(gateway, live)
    return await asyncio.to_thread(_commit_pack, work)


async def _split_stages(gateway: Structured, live: list[_Table]) -> None:
    """The four split stages for `live`, each once for all of them."""
    if not live:
        return
    found = await suggest_keys_pack(
        gateway, [KeysRequest(t.table, t.ctx, t.profiling) for t in live]
    )
    fk_names = [_apply_keys(t, keys) for t, keys in zip(live, found, strict=True)]

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


async def _merged_stage(gateway: Structured, live: list[_Table]) -> list[_Table]:
    """One `table_semantics` call for each table; returns the tables left to the split stages
    (`compile.merged`)."""
    left: list[_Table] = []
    for t in live:
        found = await table_semantics(gateway, t.table, t.ctx, t.profiling)
        if found is None:
            left.append(t)
            continue
        _apply_keys(t, found.keys)
        t.specs, t.terms = found.specs, found.terms
        if not t.specs:
            logger.warning("[%s] has no non-FK columns; no Term", t.name)
            t.skipped = "no non-FK columns"
    return left


def _apply_keys(t: _Table, keys: TableKeys) -> set[str]:
    """Decide whether the table is a junction; returns its FK columns, declared and suggested."""
    names = fk_source_columns(t.ctx.get("fks", [])) | {s.column_name for s in keys.suggestions}
    # Payload-bearing junctions are wider than the two-key shape the bridge pass handles, so the
    # model's decision needs structural evidence of two FK roles too.
    t.junction = len(t.ctx.get("columns", [])) > 2 and keys.is_junction_table and len(names) >= 2
    return names


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
