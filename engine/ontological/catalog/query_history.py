# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/sql_parse.py
# (`populate_queries`, `parse_queries_df`, `_try_merge_with_graph`) and catalog/store/queries.py
# (`add_query`, `update_counters_and_timestamps_for_query_and_affected_data`). Parsing runs in the
# parse pool; the matching and writes run here, in one transaction.

"""Ingest a database's query history: parse it in the parse pool, write it through the store.

Upstream, per chunk of 500 statements: parse each one, look for it in the store (by text, then
by shape), then among the chunk's statements, and write the new ones one link at a time, each
link in a transaction of its own. Here:

1. Statements with the same text are counted once (`Tally`): a history repeats itself.
2. The pool parses the distinct texts and folds those with one shape together (`sql_parse`).
3. The engine folds the chunks' statements together by shape, then matches each against the
   store: by text, then by upstream's five gates and the normalized SQL, which the pool computes
   for the stored candidates.
4. One transaction writes it all: new statements and their table, column, JOIN and UNION links,
   added counts and later timestamps on statements the store held, and the months
   (`app.sql_query_month`), which upstream wrote under a key no reader parsed (`query_stats`).

The engine's own work is dict lookups and batched inserts, so the API stays responsive while the
pool parses.
"""

# The vendored store and SQLAlchemy's Core are partly untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Column,
    Date,
    Integer,
    MetaData,
    Table,
    Text,
    distinct,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert

from auto_ontology.catalog.store.queries import load_sqls_to_tables
from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store, write_transaction
from ontological.catalog.parse_pool import ParsePool
from ontological.catalog.parsers.catalog_index import CatalogIndex, SchemaEntry, TableEntry
from ontological.catalog.query_stats import Tally
from ontological.catalog.sql_parse import Link, ParsedQuery, Prefilter, Shape

logger = logging.getLogger(__name__)

#: Rows per multi-row INSERT.
BATCH = 1000

sql_query_month = Table(
    "sql_query_month",
    MetaData(schema="app"),
    Column("sql_query_id", Text, primary_key=True),
    Column("month", Date, primary_key=True),
    Column("count", Integer, nullable=False),
)


@dataclass(frozen=True)
class HistoryRow:
    """One query-history record, with upstream's `get_queries` columns: the statement, when it
    ended, and how many times it ran (1 when the source doesn't say)."""

    query_text: str
    end_time: datetime
    count: int = 1


@dataclass(frozen=True)
class HistoryIngested:
    database_name: str
    rows: int  # records read
    added: int  # statements new to the store
    merged: int  # statements the store already held, whose counts grew
    unmatched: int  # distinct texts that touch no table in the catalog
    failed: int  # distinct texts that didn't parse


class NotInCatalog(LookupError):
    """The database's catalog isn't in the store, so its history has nothing to resolve against."""


def ingest_query_history(
    database_name: str,
    history: Iterable[HistoryRow],
    *,
    dialects: Sequence[str],
    pool: ParsePool,
) -> HistoryIngested:
    """Parse `history` against `database_name`'s catalog in `pool`, and write it to the store."""
    tallies: dict[str, Tally] = {}
    rows = 0
    for row in history:
        rows += 1
        tallies.setdefault(row.query_text, Tally()).add(row.count, row.end_time)

    catalog = read_catalog_index(database_name)
    parsed, unmatched, failed = _parse(list(tallies), catalog, dialects, pool)
    matched, new = _match_stored(parsed, dialects, pool)

    def tally(q: ParsedQuery) -> Tally:
        out = Tally()
        for text in q.texts:
            out.merge(tallies[text])
        return out

    with write_transaction():
        ids = {id(q): str(uuid.uuid4()) for q in new}
        _insert_statements([(ids[id(q)], q, tally(q)) for q in new])
        _add_counts([(sql_id, tally(q)) for sql_id, q in matched])
        _write_months(
            [(ids[id(q)], tally(q)) for q in new] + [(sql_id, tally(q)) for sql_id, q in matched]
        )
    logger.info(
        "%s: %d history rows, %d distinct; %d statements added, %d already held, %d touch no "
        "catalog table, %d failed to parse",
        database_name,
        rows,
        len(tallies),
        len(new),
        len(matched),
        unmatched,
        failed,
    )
    return HistoryIngested(database_name, rows, len(new), len(matched), unmatched, failed)


def read_catalog_index(database_name: str) -> CatalogIndex:
    """`database_name`'s schemas, tables and columns, names lower-cased, for the parse pool."""
    d, sc, t, c = s.catalog_database, s.catalog_schema, s.catalog_table, s.catalog_column
    found = store().query_read(select(d.c.id).where(d.c.name == database_name))
    if not found:
        raise NotInCatalog(f"{database_name} isn't in the catalog; ingest its catalog first")
    rows = store().query_read(
        select(
            sc.c.name.label("schema"),
            t.c.id.label("table_id"),
            t.c.name.label("table"),
            c.c.id.label("column_id"),
            c.c.name.label("column"),
        )
        .select_from(sc.join(t, t.c.schema_id == sc.c.id).outerjoin(c, c.c.table_id == t.c.id))
        .where(sc.c.database_id == found[0]["id"])
        .order_by(sc.c.name, t.c.name, c.c.ordinal_position, c.c.name)
    )
    schemas: dict[str, SchemaEntry] = {}
    for r in rows:
        schema = schemas.setdefault(str(r["schema"]).lower(), SchemaEntry())
        table = schema.tables.setdefault(
            str(r["table"]).lower(), TableEntry(r["table_id"], r["table"])
        )
        if r["column_id"] is not None:
            table.columns.setdefault(str(r["column"]).lower(), r["column_id"])
    return CatalogIndex(schemas)


def _parse(
    texts: list[str], catalog: CatalogIndex, dialects: Sequence[str], pool: ParsePool
) -> tuple[list[ParsedQuery], int, int]:
    """The pool's parse of `texts`, its chunks' statements folded together by shape."""
    parsed: list[ParsedQuery] = []
    by_shape: dict[Shape, ParsedQuery] = {}
    unmatched = failed = 0
    for chunk in pool.parse(texts, catalog, dialects):
        unmatched += chunk.unmatched
        failed += chunk.failed
        for text, error in chunk.failures:
            logger.info("Failed parsing query %r: %s", text, error)
        for q in chunk.parsed:
            shape = q.shape
            if shape is not None and shape in by_shape:
                by_shape[shape].texts.extend(q.texts)
                continue
            if shape is not None:
                by_shape[shape] = q
            parsed.append(q)
    return parsed, unmatched, failed


def _match_stored(
    parsed: list[ParsedQuery], dialects: Sequence[str], pool: ParsePool
) -> tuple[list[tuple[str, ParsedQuery]], list[ParsedQuery]]:
    """Upstream's `_try_merge_with_graph`: each statement the store already holds, by text or by
    shape, paired with its id; and the statements it doesn't."""
    stored = load_sqls_to_tables()
    by_text: dict[str, str] = {}
    by_prefilter: dict[Prefilter, list[tuple[str, str]]] = {}
    for r in stored.to_dict(orient="records"):
        by_text[r["sql_full_query"]] = r["sql_id"]
        if r["nodes_count"] is None:
            continue
        key: Prefilter = (
            frozenset(r["tbls"]),
            frozenset(r["cols"]),
            int(r["nodes_count"]),
            int(r["join_count"]),
            int(r["union_count"]),
        )
        by_prefilter.setdefault(key, []).append((r["sql_id"], r["sql_full_query"]))

    matched: list[tuple[str, ParsedQuery]] = []
    unsure: list[ParsedQuery] = []
    for q in parsed:
        exact = next((by_text[t] for t in q.texts if t in by_text), None)
        if exact is not None:
            matched.append((exact, q))
        elif q.normalized is not None and q.prefilter in by_prefilter:
            unsure.append(q)

    # The stored candidates' normalized SQL, from the pool, each candidate once.
    candidates = sorted({c for q in unsure for c in by_prefilter[q.prefilter]})
    normalized = dict(
        zip(candidates, pool.normalize([text for _, text in candidates], dialects), strict=True)
    )
    for q in unsure:
        same = (c for c in by_prefilter[q.prefilter] if normalized[c] == q.normalized)
        hit = next((sql_id for sql_id, _ in same), None)
        if hit is not None:
            matched.append((hit, q))

    held = {id(q) for _, q in matched}
    return matched, [q for q in parsed if id(q) not in held]


def _batches[T](items: Sequence[T]) -> Iterable[Sequence[T]]:
    for i in range(0, len(items), BATCH):
        yield items[i : i + BATCH]


def _insert(table: Table, values: list[dict[str, Any]]) -> None:
    for batch in _batches(values):
        store().query_write(insert(table).values(list(batch)))


def _insert_statements(new: list[tuple[str, ParsedQuery, Tally]]) -> None:
    """Upstream's `add_query` for new statements: the statement, then its links."""
    _insert(
        s.sql_query,
        [
            {
                "id": sql_id,
                "sql_full_query": q.sql_text,
                "total_counter": t.total,
                "last_query_timestamp": t.last,
                "nodes_count": q.nodes_count,
                "join_count": q.join_count,
                "union_count": q.union_count,
            }
            for sql_id, q, t in new
        ],
    )
    _insert(
        s.sql_query__table,
        [{"sql_query_id": sql_id, "table_id": tid} for sql_id, q, _ in new for tid in q.table_ids],
    )
    _insert(
        s.sql_query__column,
        [
            {"sql_query_id": sql_id, "column_id": cid}
            for sql_id, q, _ in new
            for cid in q.column_ids
        ],
    )
    _write_links(s.column__join, [(sql_id, link) for sql_id, q, _ in new for link in q.joins])
    _write_links(s.column__union, [(sql_id, link) for sql_id, q, _ in new for link in q.unions])


def _write_links(table: Table, links: list[tuple[str, Link]]) -> None:
    """Column pairs with their `refs` (`<statement id>|<condition>`), which accumulate: a pair
    another statement made already keeps its refs and gains these, without repeats."""
    refs: dict[tuple[str, str], list[str]] = {}
    for sql_id, link in links:
        pair = refs.setdefault((link.source_column_id, link.target_column_id), [])
        ref = f"{sql_id}|{link.condition}"
        if ref not in pair:
            pair.append(ref)
    values = [
        {"source_column_id": src, "target_column_id": dst, "refs": r}
        for (src, dst), r in refs.items()
    ]
    for batch in _batches(values):
        statement = insert(table).values(list(batch))
        merged = table.c.refs + statement.excluded.refs
        statement = statement.on_conflict_do_update(
            index_elements=["source_column_id", "target_column_id"],
            set_={
                "refs": select(
                    func.array_agg(distinct(func.unnest(merged).column_valued("ref")))
                ).scalar_subquery()
            },
        )
        store().query_write(statement)


def _add_counts(matched: list[tuple[str, Tally]]) -> None:
    """Upstream's `update_counters_and_timestamps_for_query_and_affected_data`: the sightings
    added to the statement's total, and its timestamp moved to the latest. Upstream set the
    timestamp to the incoming one, even when it was older."""
    q = s.sql_query
    for sql_id, t in matched:
        store().query_write(
            update(q)
            .where(q.c.id == sql_id)
            .values(
                total_counter=q.c.total_counter + t.total,
                last_query_timestamp=func.greatest(q.c.last_query_timestamp, t.last),
            )
        )


def _write_months(statements: list[tuple[str, Tally]]) -> None:
    """Each statement's counts added to its months. One INSERT can't touch a row twice, so a
    statement listed twice is summed first."""
    counts: dict[tuple[str, date], int] = {}
    for sql_id, t in statements:
        for month, count in t.months.items():
            counts[(sql_id, month)] = counts.get((sql_id, month), 0) + count
    values = [
        {"sql_query_id": sql_id, "month": month, "count": count}
        for (sql_id, month), count in counts.items()
    ]
    for batch in _batches(values):
        statement = insert(sql_query_month).values(list(batch))
        store().query_write(
            statement.on_conflict_do_update(
                index_elements=["sql_query_id", "month"],
                set_={"count": sql_query_month.c.count + statement.excluded.count},
            )
        )


def month_counts(sql_ids: Iterable[str]) -> dict[str, dict[date, int]]:
    """Each statement's counts by month (the month's first day), for usage scoring."""
    rows = store().query_read(
        select(sql_query_month).where(sql_query_month.c.sql_query_id.in_(list(sql_ids)))
    )
    out: dict[str, dict[date, int]] = {}
    for r in rows:
        out.setdefault(r["sql_query_id"], {})[r["month"]] = r["count"]
    return out
