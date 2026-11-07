# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/sql_attribute_suggester.py
# at 2a24f5d. The prompt is upstream's, sent through ClaudeGateway.structured() on the
# `sql_attribute_judge` route. `_usage_score` is fixed: upstream's returns 0, so its "top 10" was
# insertion order. Upstream asks once per Term, four Terms at a time; here several Terms share a
# call, one call at a time, and a Term the answer leaves out is asked alone. Fragment extraction
# runs in the parse pool.

"""The SQL-attribute stage: named SQL from query history (ARCHITECTURE.md §1.3 post-stages, §6.4).

For each Term with query history on its tables:

1. the parse pool pulls the fragments out of each statement (`compile.fragments`);
2. each fragment scores the uses of the statements it appears in over the last three months
   (`usage_score`), and the Term's top 10 go to the judge;
3. `sql_attribute_judge` keeps the fragments worth naming, and names and describes them, for
   several Terms a call;
4. each kept fragment is wrapped in a SELECT built from a statement it came from, its tables
   schema-qualified, and written under the Term (`compile.sql_attribute_writes`), unless its
   name or its SQL is taken or it doesn't resolve against the catalog.

**The `_usage_score` fix.** Upstream meant to rank fragments by 3-month execution counts, but
its per-month counters were written under a key no reader parsed, so every score was 0
(`catalog.query_stats`). M2.4 keeps the months in `app.sql_query_month`. A statement's score
here is its count over the three calendar months ending with the latest month the database's
history was seen in, and a fragment's is the sum over its statements. The window is anchored to
the history, not to today, so a history read long ago still ranks; and a statement busy two
years ago and idle since doesn't outrank one used this month. All-time counts break ties.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from auto_ontology.dal.terms import fetch_table_schema_map, fetch_terms_with_sqls
from auto_ontology.semantic.constants import SEMANTIC_SOURCE
from ontological.catalog.parse_pool import ParsePool
from ontological.catalog.query_history import month_counts, read_catalog_index
from ontological.compile import packed
from ontological.compile.fragments import NoContext, build_valid_sql
from ontological.compile.gateway import Structured, ask
from ontological.compile.models import (
    PackedSqlAttributeSuggestions,
    SqlAttributeSuggestions,
    SuggestedSqlAttribute,
    TermSuggestionsEntry,
)
from ontological.compile.sql_attribute_writes import create_sql_attribute

logger = logging.getLogger(__name__)

TASK = "sql_attribute_judge"
PROMPT_VERSION = "2a24f5d"
# The batched prompt is Ontological's, so it has a version of its own.
BATCH_PROMPT_VERSION = "2a24f5d+judge.1"

#: Upstream's: the fragments a Term sends to the judge.
TOP_N = 10
#: The most Terms one call judges.
BATCH_TERMS = 10
#: The most characters of Term sections one call carries, about 6k tokens.
BATCH_CHARS = 24_000
#: Months in the usage window.
USAGE_MONTHS = 3

SYSTEM = (
    "You are an expert data engineer reviewing SQL expressions to decide "
    "which ones represent reusable business logic worth naming. "
    "A SqlAttribute is a named, reusable SQL expression — a filter, "
    "aggregation, or calculation — that captures a clear business concept "
    "and is specific enough to be useful while general enough to recur."
)

RULES = (
    "Rules you MUST follow:\n"
    "  1. ALWAYS mark an expression as a SqlAttribute if it compares a column "
    "against a literal value (e.g. status = 'active', amount > 1000, "
    "type IN ('A','B')). Literal values encode business thresholds and "
    "categories that are exactly what SqlAttributes are designed to capture. "
    "Exception: do NOT promote an expression whose only literal is an integer "
    "that looks like a surrogate or primary-key identifier "
    "(e.g. request_id = 50000, id = 12). Such values are instance-specific "
    "record lookups, not reusable business logic. Only promote an expression "
    "containing such an integer if the surrounding context also encodes a "
    "reusable business rule (e.g. the same WHERE clause additionally filters "
    "on a status, category, or date range).\n"
    "  2. NEVER mark an expression as a SqlAttribute if it only references "
    "columns from the same table with no literal value, constant, or "
    "cross-table condition (e.g. start_date < end_date). Such expressions "
    "describe structural integrity, not reusable business logic.\n"
    "  4. NEVER mark an IS NULL / IS NOT NULL check on a surrogate or "
    "primary-key column (any column named 'id' or ending in '_id') as a "
    "SqlAttribute. These are LEFT JOIN absence checks — structural plumbing "
    "that detects whether a join matched, not a business concept "
    "(e.g. request_task_collaborators.id IS NULL). "
    "Exception: IS NULL on non-key columns that carry business meaning IS "
    "valid (e.g. deleted_at IS NULL, approved_by IS NULL).\n"
    "  3. ALWAYS mark a JOIN ON condition or a UNION/INTERSECT/EXCEPT branch "
    "as a SqlAttribute when it appears frequently AND any of the following "
    "is true:\n"
    "     a. It encodes obvious business logic — "
    "e.g. a join that filters to active records, a union arm that defines a "
    "named sub-population, or a cross-table condition capturing a business "
    "relationship.\n"
    "     b. It is used to define a meaningful business alias — e.g. a SELECT "
    "branch or subquery whose result column is aliased with a descriptive "
    "business name (e.g. 'AS active_assignees', 'AS overdue_tasks'). "
    "The alias itself signals that the expression represents a named concept "
    "worth capturing.\n"
    "     c. A UNION combines two or more SELECT branches that each use a "
    "different join path to reach the same entity type (e.g. one branch joins "
    "via a direct ownership FK, another via a membership/collaborator table). "
    "This pattern structurally defines a named aggregate concept — all members "
    "of a group regardless of their role. Promote the whole UNION as a "
    "SqlAttribute representing that concept (e.g. 'Task Participants', "
    "'Order Stakeholders'). When naming and describing it, ignore any "
    "instance-specific WHERE predicates (like record-ID literals) and focus "
    "on the reusable structural pattern.\n"
    "     Ignore pure FK-only joins and UNION branches that select from a "
    "single table with no join or filter.\n\n"
    "For each chosen expression provide:\n"
    "  • name — user-friendly Title Case label with spaces between words, "
    "matching the ColumnAttribute naming style "
    "(e.g. 'Active Customer Filter', 'Revenue Above Threshold', "
    "'Last 90 Days Activity')\n"
    "  • description — what business logic it captures, why it matters for "
    "this term, and when a data consumer would apply it\n"
    "  • expression — the exact SQL expression copied verbatim\n\n"
    "Return an empty list if none qualify."
)

BATCH_NOTE = (
    "\n\nThis request covers several business terms. Each one starts with its own "
    '"Business term:" line, followed by its description and its expressions. Judge each '
    "term's expressions for that term on its own, and return one entry per term in `terms`, "
    'with `term` set to the name exactly as written after "Business term:".'
)


@dataclass(frozen=True)
class Ranked:
    expression: str
    score: int  # uses in the window, summed over its statements
    total: int  # all-time uses, summed over its statements
    sql_ids: list[str]


@dataclass
class TermWork:
    term_id: str
    name: str
    description: str
    top: list[Ranked]
    source_sql: dict[str, list[str]]  # each ranked expression's statements' texts


@dataclass(frozen=True)
class SqlAttributes:
    terms: int  # Terms with history and something to judge
    calls: int  # judge calls asked
    suggested: int  # fragments the judge kept
    created: list[str] = field(default_factory=list[str])  # names written
    skipped: int = 0  # kept, but not written: no SELECT, a conflict, or unresolved


def usage_window(months: Iterable[date]) -> date | None:
    """The first month of the `USAGE_MONTHS` calendar months that end with the latest of
    `months`, or None when there are none."""
    latest = max(months, default=None)
    if latest is None:
        return None
    index = latest.year * 12 + latest.month - 1 - (USAGE_MONTHS - 1)
    return date(index // 12, index % 12 + 1, 1)


def usage_score(months: Mapping[date, int], since: date | None) -> int:
    """A statement's uses from `since` on: upstream's `_usage_score`, which returned 0."""
    if since is None:
        return 0
    return sum(count for month, count in months.items() if month >= since)


def rank_expressions(
    sqls: Sequence[dict[str, Any]],
    fragments: Mapping[str, list[str]],
    usage: Mapping[str, int],
) -> list[Ranked]:
    """Upstream's `_rank_expressions`, with real scores: each fragment of the Term's statements
    by its window uses, then its all-time uses, then first appearance."""
    order: list[str] = []
    score: dict[str, int] = {}
    total: dict[str, int] = {}
    ids: dict[str, list[str]] = {}
    for item in sqls:
        text, sql_id = item.get("sql_text") or "", str(item.get("sql_id") or "")
        if not text:
            continue
        props = item.get("props") or {}
        for expr in fragments.get(text, []):
            if expr not in score:
                order.append(expr)
                score[expr], total[expr], ids[expr] = 0, 0, []
            score[expr] += usage.get(sql_id, 0)
            total[expr] += int(props.get("total_counter") or 0)
            if sql_id and sql_id not in ids[expr]:
                ids[expr].append(sql_id)
    ranked = sorted(order, key=lambda e: (-score[e], -total[e]))
    return [Ranked(e, score[e], total[e], ids[e]) for e in ranked]


def _head(name: str, description: str, top: Sequence[Ranked]) -> str:
    block = "\n".join(f"{i + 1}. [{r.score:.0f} uses] {r.expression}" for i, r in enumerate(top))
    return (
        f"Business term: {name}\n"
        f"Description: {description or '(none)'}\n\n"
        f"Top SQL expressions extracted from queries on tables for this term "
        f"(ranked by 3-month execution count):\n\n"
        f"{block}"
    )


def build_prompt(name: str, description: str, top: Sequence[Ranked]) -> str:
    """Upstream's prompt for one Term."""
    return (
        f"{_head(name, description, top)}\n\n"
        f"Which expressions should become a named SqlAttribute for the term "
        f"'{name}'?\n\n" + RULES
    )


def build_batch_prompt(terms: Sequence[TermWork]) -> str:
    """Upstream's prompt for several Terms: each Term's heading and expressions, then the
    question and the rules once."""
    heads = "\n\n".join(_head(t.name, t.description, t.top) for t in terms)
    return (
        f"{heads}\n\n"
        "For each business term above, which of its expressions should become a named "
        "SqlAttribute for that term?\n\n" + RULES
    )


def batches(terms: Sequence[TermWork]) -> list[list[TermWork]]:
    """The Terms in order, at most `BATCH_TERMS` and about `BATCH_CHARS` of prompt to a batch."""
    out: list[list[TermWork]] = []
    size = 0
    for t in terms:
        length = len(_head(t.name, t.description, t.top))
        if out and len(out[-1]) < BATCH_TERMS and size + length <= BATCH_CHARS:
            out[-1].append(t)
            size += length
        else:
            out.append([t])
            size = length
    return out


async def judge_term(gateway: Structured, term: TermWork) -> list[SuggestedSqlAttribute]:
    """Upstream's `_judge_with_llm`: the fragments to name; none for an invalid answer. Raises
    `ClaudeUnavailable` when Claude doesn't answer."""
    result = await ask(
        gateway,
        TASK,
        SYSTEM,
        build_prompt(term.name, term.description, term.top),
        SqlAttributeSuggestions,
        PROMPT_VERSION,
    )
    return [] if result is None else result.suggestions


async def judge_batch(
    gateway: Structured, terms: Sequence[TermWork]
) -> tuple[list[list[SuggestedSqlAttribute]], int]:
    """Each Term's suggestions, from one call when there are several Terms; a Term the answer
    leaves out, or every Term of an invalid answer, is asked alone. Also the calls it made."""
    if len(terms) == 1:
        return [await judge_term(gateway, terms[0])], 1
    result = await ask(
        gateway,
        TASK,
        SYSTEM + BATCH_NOTE,
        build_batch_prompt(terms),
        PackedSqlAttributeSuggestions,
        BATCH_PROMPT_VERSION,
    )
    calls = 1
    found: dict[str, TermSuggestionsEntry] = {}
    if result is not None:
        found = packed.match(result.terms, [t.name for t in terms], lambda e: e.term)
    out: list[list[SuggestedSqlAttribute]] = []
    for t in terms:
        entry = found.get(t.name)
        if entry is None:
            out.append(await judge_term(gateway, t))
            calls += 1
        else:
            out.append(entry.suggestions)
    return out, calls


def _work(database_name: str, pool: ParsePool | None) -> list[TermWork]:
    rows = fetch_terms_with_sqls(SEMANTIC_SOURCE, database_name=database_name)
    if not rows:
        return []
    texts = list(
        dict.fromkeys(item["sql_text"] for row in rows for item in row["sqls"] if item["sql_text"])
    )
    if pool is not None:
        found = pool.fragments(texts)
    else:
        with ParsePool() as own:
            found = own.fragments(texts)
    fragments = dict(zip(texts, found, strict=True))

    ids = {str(item["sql_id"]) for row in rows for item in row["sqls"] if item.get("sql_id")}
    months = month_counts(ids)
    since = usage_window(m for counts in months.values() for m in counts)
    usage = {sql_id: usage_score(counts, since) for sql_id, counts in months.items()}

    work: list[TermWork] = []
    for row in rows:
        sqls = row.get("sqls") or []
        ranked = rank_expressions(sqls, fragments, usage)
        top = ranked[:TOP_N]
        if not top:
            continue
        text_by_id = {str(i["sql_id"]): i["sql_text"] for i in sqls if i.get("sql_text")}
        work.append(
            TermWork(
                term_id=str(row["term_id"]),
                name=str(row["term_name"]),
                description=str(row.get("term_description") or ""),
                top=top,
                source_sql={
                    r.expression: [text_by_id[i] for i in r.sql_ids if i in text_by_id]
                    for r in ranked
                },
            )
        )
    return work


def _write(
    database_name: str,
    judged: Sequence[tuple[TermWork, list[SuggestedSqlAttribute]]],
    seen: set[str],
) -> tuple[list[str], int]:
    """Write a batch's suggestions. `seen` holds the expressions this run has handled, so two
    Terms on one table don't each get the same one."""
    schema_map = fetch_table_schema_map(database_name)
    catalog = read_catalog_index(database_name)
    created: list[str] = []
    skipped = 0
    for term, suggestions in judged:
        for suggestion in suggestions:
            key = suggestion.expression.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                sql = build_valid_sql(
                    suggestion.expression,
                    term.source_sql.get(suggestion.expression, []),
                    schema_map,
                )
            except NoContext as e:
                logger.info("Term %r: %s", term.name, e)
                skipped += 1
                continue
            outcome = create_sql_attribute(
                name=suggestion.name,
                description=suggestion.description,
                expression=sql,
                term_id=term.term_id,
                source="sql",
                catalog=catalog,
            )
            if outcome == "created":
                logger.info("Term %r: SQL attribute %r", term.name, suggestion.name)
                created.append(suggestion.name)
            else:
                logger.info(
                    "Term %r: SQL attribute %r not written: %s", term.name, suggestion.name, outcome
                )
                skipped += 1
    return created, skipped


async def suggest_sql_attributes(
    database_name: str, gateway: Structured, *, pool: ParsePool | None = None
) -> SqlAttributes:
    """Judge every Term's query-history fragments and write the SQL attributes the judge names.
    Each batch's attributes are written once it is answered, so a stop keeps them, and a rerun
    finds their names taken. Fragments are extracted in `pool`, or in a pool of its own. Raises
    `ClaudeUnavailable` when Claude doesn't answer."""
    work = await asyncio.to_thread(_work, database_name, pool)
    if not work:
        logger.info("%s: no Term has query history to judge", database_name)
        return SqlAttributes(0, 0, 0)
    calls = suggested = skipped = 0
    created: list[str] = []
    seen: set[str] = set()
    for batch in batches(work):
        answers, asked = await judge_batch(gateway, batch)
        calls += asked
        suggested += sum(len(a) for a in answers)
        names, missed = await asyncio.to_thread(
            _write, database_name, list(zip(batch, answers, strict=True)), seen
        )
        created.extend(names)
        skipped += missed
    logger.info(
        "%s: %d Term(s) judged in %d call(s); %d SQL attribute(s) written, %d not",
        database_name,
        len(work),
        calls,
        len(created),
        skipped,
    )
    return SqlAttributes(len(work), calls, suggested, created, skipped)
