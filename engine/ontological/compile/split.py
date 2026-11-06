# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/compile.py, pipeline.py
# and visit_enter.py (`process_table`, `_commit_terms`) at 2a24f5d. Tables run one at a time,
# in upstream's canonical commit order, and so do the Claude calls. A table's terms and
# attributes are written in one transaction. The FK stage reads the table's columns after
# profiling has stored their samples, where upstream reads them before, so its prompt has
# samples on a first compile. Embedding, the SQL-attribute and bridge stages are later work.

"""The minimal split compile (BUILD_PLAN.md M1.4; ARCHITECTURE.md §1.3, §6.4).

For each table with no Term yet, in canonical order:

1. profile it through the source executor, and store samples, uniqueness and date notations;
2. `table_keys`: suggest FKs and PKs and decide whether it is a junction table;
3. `column_descriptions`: describe its undescribed non-FK columns, 15 to a call;
4. `table_terms`: propose its Terms and assign every non-FK column to one;
5. write the Terms and their attributes.

Then semantic FKs from the declared foreign keys. Every Claude call goes through the gateway,
one at a time. An answer that fails validation takes upstream's deterministic fallback. When
Claude doesn't answer at all (a rate limit, an overload, a logged-out or missing `claude`), the
compile stops before writing that table, so it and the tables after it stay in the work list for
the next run, which resumes there.
Packing small tables, chunking wide ones, checkpoints and the `merged` option are M3's.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

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
from auto_ontology.dal.session import write_transaction
from auto_ontology.dal.terms import upsert_table_term
from ontological.compile.descriptions import column_attribute_specs
from ontological.compile.gateway import ClaudeUnavailable, Structured
from ontological.compile.keys import fk_source_columns, suggest_keys
from ontological.compile.models import ColumnAttributeSpec, TermProposal
from ontological.compile.semantic_fk import SemanticFks, resolve_declared_fks
from ontological.compile.terms import extract_terms
from ontological.profile.sample import TableProfile, profile_table
from ontological.sources.executor import SourceExecutor

logger = logging.getLogger(__name__)


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
    tables: list[CompiledTable]  # the tables written, or skipped, before any stop
    semantic_fks: SemanticFks | None  # None when the compile stopped
    stopped: ClaudeUnavailable | None = None  # why it stopped; the rest is left for the next run

    @property
    def complete(self) -> bool:
        return self.stopped is None


def commit_key(database_name: str, table: dict[str, Any]) -> tuple[str, ...]:
    """Upstream's canonical table order, which decides who keeps an unqualified Term name when
    two tables propose the same one."""
    parts = (database_name, str(table.get("schema_name") or ""), str(table.get("name") or ""))
    return (*[p.casefold() for p in parts], *parts, str(table.get("id") or ""))


async def compile_database(
    database_name: str,
    executor: SourceExecutor,
    gateway: Structured,
    *,
    probe_distinct_values: bool = True,
) -> Compiled:
    """Compile every table of `database_name` that has no Term yet, then its semantic FKs.
    `executor` holds the database's source. When Claude stops answering, returns what was done,
    with `stopped` set, and writes nothing for the table it was on."""
    tables = sorted(
        await asyncio.to_thread(fetch_all_tables_without_term, database_name),
        key=lambda t: commit_key(database_name, t),
    )
    logger.info("compiling %d table(s) of %s", len(tables), database_name)
    done: list[CompiledTable] = []
    for position, table in enumerate(tables, start=1):
        logger.info("[%d/%d] %s", position, len(tables), table["name"])
        try:
            compiled = await compile_table(
                table, executor, gateway, probe_distinct_values=probe_distinct_values
            )
        except ClaudeUnavailable as e:
            logger.warning(
                "compile of %s stopped at %s, %d table(s) left: %s",
                database_name,
                table["name"],
                len(tables) - position + 1,
                e,
            )
            return Compiled(database_name, done, None, stopped=e)
        done.append(compiled)
    fks = await asyncio.to_thread(resolve_declared_fks, database_name)
    return Compiled(database_name, done, fks)


async def compile_table(
    table: dict[str, Any],
    executor: SourceExecutor,
    gateway: Structured,
    *,
    probe_distinct_values: bool = True,
) -> CompiledTable:
    table_id, name = str(table["id"]), str(table["name"])
    loaded = await asyncio.to_thread(_profile, table, executor, probe_distinct_values)
    if loaded is None:
        logger.warning("[%s] has no columns; skipping", name)
        return CompiledTable(table_id, name, skipped="no columns")
    table, ctx, profile = loaded
    profiling = profile.as_prompt_input()

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
        if junction:
            await asyncio.to_thread(mark_table_as_junction, table_id)
        logger.warning("[%s] has no non-FK columns; no Term", name)
        return CompiledTable(table_id, name, junction=junction, skipped="no non-FK columns")

    terms = await extract_terms(gateway, table, specs)
    written, attrs = await asyncio.to_thread(_commit, table_id, terms, specs, junction)
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
        store_column_sample_values(table_id, profile.stored_samples)
        store_column_uniqueness(table_id, profile.uniqueness)
        store_column_date_formats(table_id, profile.date_formats)
    row = fetch_table_by_id(table_id) or {}
    return {**table, "pk": row.get("pk") or []}, fetch_table_context(table_id), profile


def _commit(
    table_id: str, terms: list[TermProposal], specs: list[ColumnAttributeSpec], junction: bool
) -> tuple[list[str], list[str]]:
    """Write the Terms and their attributes, in one transaction. A Term whose name another table
    holds is written under a qualified name."""
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
    return written, attrs
