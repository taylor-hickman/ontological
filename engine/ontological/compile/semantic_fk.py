# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/semantic_fk.py at
# 2a24f5d. The pick is asked for about 25 columns a call on the `semantic_fk_pick` route, where
# upstream asks once a column from two threads; a column alone in its batch, or one the batch's
# answer leaves out or answers with an id it wasn't offered, gets upstream's prompt unchanged.
# Candidates come from the local embedder's dense search of the semantic layer. Every pick is
# made before any inferred edge is written, so a compile that stops on Claude asks the same
# batches when it resumes. The sample-value match and the overlap check run through the source
# executor's `probe` op, in DuckDB's SQL, and the overlap check reads both counts from one row,
# where upstream's UNION ALL relies on its rows coming back in order. Join defaults take
# upstream's deterministic fallback only.

"""Semantic FKs (ARCHITECTURE.md §1.3 post-stages, §6.4).

A column that is a foreign key carries no attribute of its own: the compile stages left it out.
Here it gets a `column__semantic_fk` edge to the attribute of the column it references, which is
what join paths (`find_join_path`) walk.

1. A declared FK gets its edge straight away, unless it points into its own table: that is a
   self-reference, never the entity it points to.
2. Every other unlinked column, in canonical order, gets candidates from two dense searches of
   the semantic layer (name and description, then name alone, 3 each): unique attributes on
   other tables.
3. Claude picks each column's referenced attribute from its candidates, or none, 25 columns to a
   `semantic_fk_pick` call.
4. For a column Claude passed on, a probe looks for a same-named candidate whose column holds
   every one of its sample values.
5. Every edge whose column shares no value with its target, or holds only NULLs, is deleted.
6. Each source table gets one join default per target attribute.

Without a search there are no candidates, and only the declared FKs get edges: compile passes
none without an embedder, or when the job compiled no table. Steps 4 and 5 query the source
through the executor's `probe` op; a probe the source refuses or that times out leaves the edge
as it is.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from auto_ontology.dal.attributes import (
    delete_semantic_fk,
    fetch_semantic_fk_join_groups,
    find_column_attribute_by_column_id,
    find_unlinked_fk_columns,
    merge_semantic_fk,
    set_semantic_fk_join_default,
)
from auto_ontology.dal.session import write_transaction
from auto_ontology.utils.sample_values import stringify_sample_values
from ontological.compile.gateway import Structured, ask
from ontological.sources.executor import (
    ExecutorDied,
    SourceExecutor,
    SourceQueryError,
    StatementTimeout,
)

logger = logging.getLogger(__name__)

TASK = "semantic_fk_pick"
PROMPT_VERSION = "2a24f5d"  # one column: upstream's prompt
BATCH_PROMPT_VERSION = "2a24f5d+batch.1"
BATCH_SIZE = 25  # columns to a call (§6.4)
HITS_PER_SEARCH = 3
CANDIDATE_TEXT_CHARS = 300
HEADING = "Foreign-key column: "

# A dense search of the semantic layer: query, k and a `where` filter in, `{text, metadata,
# _distance}` hits out, nearest first.
Search = Callable[[str, int, dict[str, Any]], list[dict[str, Any]]]
Column = dict[str, Any]  # a row of `find_unlinked_fk_columns`
Hit = dict[str, Any]

SYSTEM = """\
You are a database schema expert. You will be given a foreign-key column \
description and a list of candidate primary-key columns retrieved from a \
vector search. Each candidate is prefixed with its unique column_id. \
Your task is to decide which candidate (if any) is the column that the \
foreign key references.

Rules:
- Return the exact column_id of the best matching candidate, or null if none fit.
- Only pick a candidate when you are confident it is the PK being referenced.
- Do NOT guess. If unsure, return null.
"""

BATCH_NOTE = """
This request covers several foreign-key columns. Each one starts with its own \
"Foreign-key column:" line, followed by that column's details and its own candidates. Decide \
each column on its own, from its own candidates only, and return one entry per column in \
`columns`, with `column` set to the text exactly as written after "Foreign-key column:", such \
as `customer_id (table: orders)`."""


class FkHitSelection(BaseModel):
    """LLM output: selects the best matching Column hit from a VDB result list."""

    model_config = ConfigDict(extra="forbid")

    selected_id: str | None = Field(
        ...,
        description=(
            "The column_id of the candidate that is the primary-key column this FK "
            "references, exactly as shown in the candidate list. "
            "Return null if none of the candidates are a confident match."
        ),
    )
    rationale: str = Field(default="")


class FkPickEntry(FkHitSelection):
    column: str = Field(description='The column exactly as written after "Foreign-key column:".')


class BatchedFkPicks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[FkPickEntry]


@dataclass(frozen=True)
class SemanticFks:
    declared: int  # edges from declared FKs
    self_references: int  # declared FKs into their own table, which get none
    inferred: int  # edges Claude picked
    matched: int  # edges the sample-value probe found after Claude passed
    unresolved: int  # columns left without an edge
    pruned: int  # edges deleted: their column shares no value with the target


@dataclass(frozen=True)
class Asking:
    """A column with its candidates, on its way to a pick."""

    column: Column
    hits: list[Hit]

    @property
    def id(self) -> str:
        return str(self.column["id"])

    @property
    def heading(self) -> str:
        return f"{self.column.get('name', '')} (table: {self.column.get('table_name', '')})"

    def valid(self, selected: str | None) -> bool:
        return selected is not None and selected in {_hit_id(h) for h in self.hits}


async def resolve_semantic_fks(
    database_name: str,
    executor: SourceExecutor,
    gateway: Structured,
    search: Search | None,
) -> SemanticFks:
    """Steps 1 to 6 above. `search` is the semantic layer's dense search; None skips inference.
    Raises `ClaudeUnavailable` before writing any inferred edge when Claude doesn't answer."""
    declared, self_refs, queue = await asyncio.to_thread(_declared, database_name)
    inferred = matched = 0
    unresolved = len(queue)
    if queue and search is None:
        logger.info("%d column(s) without a declared FK; not inferring theirs", len(queue))
    elif queue and search is not None:
        asking = await asyncio.to_thread(_with_candidates, queue, search, database_name)
        picks = await pick(gateway, asking)
        chosen: dict[str, tuple[str, bool]] = {}  # column id: attribute id, and whether picked
        for a in asking:
            picked = picks.get(a.id)
            if picked is not None:
                chosen[a.id] = (picked, True)
                continue
            found = await asyncio.to_thread(match_by_sample_values, a.column, a.hits, executor)
            if found is not None:
                chosen[a.id] = (found, False)
        written = await asyncio.to_thread(_write, {c: attr for c, (attr, _) in chosen.items()})
        inferred = sum(1 for c in written if chosen[c][1])
        matched = len(written) - inferred
        unresolved -= len(written)
    pruned = await asyncio.to_thread(prune_disjoint, database_name, executor)
    await asyncio.to_thread(select_join_defaults, database_name)
    fks = SemanticFks(declared, self_refs, inferred, matched, unresolved, pruned)
    logger.info("semantic FKs for %s: %s", database_name, fks)
    return fks


def _declared(database_name: str) -> tuple[int, int, list[Column]]:
    """Write an edge for each unlinked column with a declared FK. Returns how many were written,
    how many were self-references, and the rest in canonical order, to infer."""
    written = self_refs = 0
    queue: list[Column] = []
    for col in find_unlinked_fk_columns(database_name):
        target: str | None = col.get("fk_target_col_id")
        if not target:
            queue.append(col)
            continue
        if col.get("fk_target_table_id") == col.get("table_id"):
            self_refs += 1
            logger.debug("%s.%s references its own table", col["table_name"], col["name"])
            continue
        attr_id = find_column_attribute_by_column_id(target)
        if attr_id and merge_semantic_fk(col["id"], attr_id):
            written += 1
        else:
            logger.info(
                "%s.%s: the column it references has no attribute; inferring",
                col["table_name"],
                col["name"],
            )
            queue.append(col)
    queue.sort(
        key=lambda c: (
            str(c.get("table_name") or "").casefold(),
            str(c.get("name") or "").casefold(),
            str(c["id"]),
        )
    )
    return written, self_refs, queue


def _with_candidates(queue: list[Column], search: Search, database_name: str) -> list[Asking]:
    """The columns that have candidates, each with them."""
    asking: list[Asking] = []
    for col in queue:
        hits = candidates(col, search, database_name)
        if hits:
            asking.append(Asking(col, hits))
        else:
            logger.info("%s.%s: no candidate attributes", col["table_name"], col["name"])
    return asking


def candidates(col: Column, search: Search, database_name: str) -> list[Hit]:
    """Upstream's two searches, name and description then name alone, merged: unique
    attributes of the database, on tables other than the column's own."""
    where = {"label": "ColumnAttribute", "database_name": database_name, "is_unique": True}
    full = search(query_text(col), HITS_PER_SEARCH, where)
    name = search(f"column_name: {col.get('name', '')}", HITS_PER_SEARCH, where)
    seen: set[str] = set()
    merged: list[Hit] = []
    own_table = col.get("table_id")
    for hit in full + name:
        hit_id = _hit_id(hit)
        table_id = (hit.get("metadata") or {}).get("table_id")
        if not hit_id or hit_id in seen or not table_id or table_id == own_table:
            continue
        seen.add(hit_id)
        merged.append(hit)
    return merged


def query_text(col: Column) -> str:
    """The embedding query for a column's context."""
    parts = [f"column_name: {col.get('name', '')}"]
    desc = (col.get("description") or "").strip()
    if desc:
        parts.append(f"description: {desc}")
    samples = _format_sample_values(col.get("sample_values"))
    if samples:
        parts.append(samples)
    return ", ".join(parts)


def prompt(a: Asking) -> str:
    """Upstream's prompt for one column."""
    col = a.column
    context = (
        f"{HEADING}{a.heading}\n"
        f"Description: {col.get('description') or '(none)'}\n"
        f"Sample values: {_format_sample_values(col.get('sample_values')) or '(none)'}"
    )
    lines = "\n".join(
        f"column_id={_hit_id(h)} | {(h.get('text') or '')[:CANDIDATE_TEXT_CHARS]}" for h in a.hits
    )
    return (
        f"{context}\n\n"
        f"Candidate PK columns (from vector search):\n{lines}\n\n"
        "Which candidate does this FK column reference? "
        "Return its exact column_id value, or null if none are a confident match."
    )


async def pick(gateway: Structured, asking: Sequence[Asking]) -> dict[str, str | None]:
    """Each column's picked attribute id, or None where Claude passed or answered badly: one
    call for each batch of `BATCH_SIZE`, in order, then upstream's call for each column of the
    batch that its answer leaves out or answers with an id it wasn't offered, or that shares
    its heading with another column of the batch. A batch of one is upstream's call. Raises
    `ClaudeUnavailable` when Claude doesn't answer."""
    picks: dict[str, str | None] = {}
    for start in range(0, len(asking), BATCH_SIZE):
        batch = list(asking[start : start + BATCH_SIZE])
        alone = batch
        if len(batch) > 1:
            alone = []
            entries = await _ask_batch(gateway, batch)
            for a in batch:
                entry = entries.get(a.heading)
                if entry is None or not (entry.selected_id is None or a.valid(entry.selected_id)):
                    alone.append(a)
                else:
                    picks[a.id] = entry.selected_id
        for a in alone:
            result = await ask(gateway, TASK, SYSTEM, prompt(a), FkHitSelection, PROMPT_VERSION)
            selected = result.selected_id if result is not None else None
            if selected is not None and not a.valid(selected):
                logger.warning("Claude picked an id it wasn't offered for %s", a.heading)
                selected = None
            picks[a.id] = selected
    return picks


async def _ask_batch(gateway: Structured, batch: list[Asking]) -> dict[str, FkPickEntry]:
    """Each heading's entry in the batch's answer; none for a heading two columns share."""
    result = await ask(
        gateway,
        TASK,
        SYSTEM + BATCH_NOTE,
        "\n\n".join(prompt(a) for a in batch) + "\n",
        BatchedFkPicks,
        BATCH_PROMPT_VERSION,
    )
    if result is None:
        return {}
    shared = {h for h, n in Counter(a.heading for a in batch).items() if n > 1}
    exact: dict[str, FkPickEntry] = {}
    folded: dict[str, FkPickEntry] = {}
    for entry in result.columns:
        exact.setdefault(entry.column, entry)
        folded.setdefault(entry.column.strip().casefold(), entry)
    found: dict[str, FkPickEntry] = {}
    for a in batch:
        entry = exact.get(a.heading) or folded.get(a.heading.strip().casefold())
        if entry is not None and a.heading not in shared:
            found[a.heading] = entry
    return found


def _write(chosen: dict[str, str]) -> set[str]:
    """Write the inferred edges, column id to attribute id, in one transaction.
    `merge_semantic_fk` refuses, and logs, an edge across databases: the search's filter scopes
    hits to the database, but the catalog has the last word. Returns the columns written."""
    with write_transaction():
        return {c for c, attr_id in chosen.items() if merge_semantic_fk(c, attr_id)}


# --- probes ------------------------------------------------------------------------------------


_PROBE_FAILED = (SourceQueryError, StatementTimeout, ExecutorDied)


def match_by_sample_values(col: Column, hits: list[Hit], executor: SourceExecutor) -> str | None:
    """The nearest candidate named like the column whose column holds every one of its distinct
    sample values, by a probe per candidate; None when none does."""
    samples = list(dict.fromkeys(stringify_sample_values(col.get("sample_values")) or []))
    name = str(col.get("name") or "").lower()
    if not samples or not name:
        return None
    matches: list[Hit] = []
    for hit in hits:
        metadata = hit.get("metadata") or {}
        if str(metadata.get("source_column") or "").lower() != name:
            continue
        query = sample_match_sql(metadata, samples)
        if query is None:
            continue
        try:
            found = executor.probe(query, max_rows=len(samples))
        except _PROBE_FAILED:
            logger.info("sample match skipped for %s", metadata.get("table_name"), exc_info=True)
            continue
        values = {str(row[0]) for row in found.rows if row and row[0] is not None}
        if set(samples) <= values:
            matches.append(hit)
    if not matches:
        return None
    best = min(matches, key=_hit_score)
    metadata = best.get("metadata") or {}
    logger.info(
        "%s.%s holds %d sample value(s) of %s.%s",
        metadata.get("table_name"),
        metadata.get("source_column"),
        len(samples),
        col.get("table_name"),
        col.get("name"),
    )
    return _hit_id(best) or None


def sample_match_sql(metadata: dict[str, Any], samples: list[str]) -> str | None:
    """Which of `samples` the candidate's column holds, compared as text."""
    table_name = metadata.get("table_name")
    column_name = metadata.get("source_column")
    if not table_name or not column_name or not samples:
        return None
    schema_name = metadata.get("schema_name")
    table = exp.Table(
        this=exp.to_identifier(str(table_name)),
        db=exp.to_identifier(str(schema_name)) if schema_name else None,
    )
    text = exp.cast(exp.column(str(column_name)), "TEXT")
    query = (
        exp.select(text.copy().as_("matched_value"))
        .distinct()
        .from_(table)
        .where(text.copy().isin(*(exp.Literal.string(v) for v in samples)))
        .limit(len(samples))
    )
    return query.sql(dialect="duckdb", identify=True)


def prune_disjoint(database_name: str, executor: SourceExecutor) -> int:
    """Delete each semantic FK whose column shares no value with its target, or is all NULL.

    An inferred edge is a guess from names and descriptions; a seven-digit school code read as
    the fourteen-digit CDS code it sits beside is one. No overlap at all is the only test: real
    references in loaded data can still miss most of their target, so a low share proves
    nothing. A column of NULLs joins to nothing, yet would still be offered as a join path.
    Returns the number of edges deleted."""
    pruned = 0
    for row in fetch_semantic_fk_join_groups(database_name):
        try:
            found = executor.probe(overlap_sql(row), max_rows=1)
        except _PROBE_FAILED:
            logger.info(
                "overlap check skipped for %s.%s",
                row["source_table_name"],
                row["source_column_name"],
                exc_info=True,
            )
            continue
        if not found.rows:
            continue
        present, shared = (int(v) for v in found.rows[0])
        if shared:
            continue
        delete_semantic_fk(row["source_column_id"], row["target_attribute_id"])
        pruned += 1
        logger.info(
            "semantic FK removed, %s: %s.%s -> %s.%s",
            "no shared values" if present else "empty source column",
            row["source_table_name"],
            row["source_column_name"],
            row["target_table_name"],
            row["target_column_name"],
        )
    return pruned


def overlap_sql(row: dict[str, Any]) -> str:
    """One row: whether the source column has a non-NULL value, and whether it shares one with
    the target column. Each stops at its first row."""
    source = _table(row.get("source_schema_name"), row["source_table_name"])
    target = _table(row.get("target_schema_name"), row["target_table_name"])
    sc = _quote(row["source_column_name"])
    tc = _quote(row["target_column_name"])
    return (
        f"SELECT (SELECT count(*) FROM (SELECT {sc} FROM {source} WHERE {sc} IS NOT NULL "
        f"LIMIT 1) AS p) AS present, (SELECT count(*) FROM (SELECT {sc} FROM {source} "
        f"WHERE {sc} IN (SELECT {tc} FROM {target}) LIMIT 1) AS s) AS shared"
    )


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table(schema_name: str | None, table_name: str) -> str:
    """The table as the executor's session names it: its catalog is the session's default."""
    return f"{_quote(schema_name)}.{_quote(table_name)}" if schema_name else _quote(table_name)


# --- join defaults -----------------------------------------------------------------------------


def select_join_defaults(database_name: str) -> int:
    """Enable exactly one semantic FK per (source table, target attribute) for join paths.
    Where several columns of a table reference the same attribute, upstream asks a model and
    falls back to the first column by name; this takes the fallback."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in fetch_semantic_fk_join_groups(database_name):
        groups.setdefault((row["source_table_id"], row["target_attribute_id"]), []).append(row)
    for (table_id, attr_id), group in groups.items():
        chosen = min(
            group,
            key=lambda r: (
                str(r.get("source_column_name") or "").casefold(),
                r["source_column_id"],
            ),
        )
        set_semantic_fk_join_default(table_id, attr_id, chosen["source_column_id"])
    return len(groups)


# --- helpers -----------------------------------------------------------------------------------


def _hit_id(hit: Hit) -> str:
    return str((hit.get("metadata") or {}).get("id") or "")


def _hit_score(hit: Hit) -> float:
    """The hit's cosine distance, nearest lowest; a missing or malformed one is the worst."""
    try:
        return float(hit.get("_distance", float("inf")))
    except (TypeError, ValueError):
        return float("inf")


def _format_sample_values(raw: Any) -> str:
    values = stringify_sample_values(raw, max_len=30)
    return "sample_values: " + ", ".join(values) if values else ""
