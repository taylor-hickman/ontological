# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/sql_parse.py. The parsing
# and the dedup rules are upstream's. They run in the parse pool's workers and return plain
# structures (`ParsedQuery`) in place of `Query` objects and their `CatalogNode` edges, and nothing
# here touches the store (ARCHITECTURE.md §5.6). Matching against what the store holds, and the
# writes, are the engine's, in `query_history.py`.

"""Parse query history against a database's catalog, and fold duplicates together.

`parse_query` is upstream's `parse_query_slim`: sqlglot pulls the tables, columns, join pairs and
union pairs out of a statement, and they are resolved to catalog ids. A statement that touches no
table the catalog holds is dropped.

Two statements are the same when they have the same tables, the same columns, the same AST size,
the same numbers of joins and unions, and the same normalized SQL (`query_comparator`: literals
neutralized, SELECT lists sorted, lower-cased). The five counts are upstream's cheap pre-filter;
the normalized text is its structural comparison. `Shape` holds all six, so a dict finds a
duplicate in place of upstream's scan of candidates.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ontological.catalog.parsers.catalog_index import CatalogIndex, TableEntry
from ontological.catalog.parsers.query_comparator import normalize_sql
from ontological.catalog.parsers.sqlglot_extractor import (
    JoinPair,
    UnionPair,
    extract_tables_and_columns,
)

#: How many failed statements a chunk reports with their error, for the engine to log.
MAX_REPORTED_FAILURES = 5


@dataclass(frozen=True)
class Link:
    """A column-to-column JOIN or UNION a statement makes, by column id. `condition` is
    upstream's text for it (`table.col = table.col`, or `table.col | table.col` for a union)."""

    source_column_id: str
    target_column_id: str
    condition: str


#: Upstream's cheap gates: tables, columns, AST nodes, joins, unions.
Prefilter = tuple[frozenset[str], frozenset[str], int, int, int]
#: The gates and the normalized SQL: two statements with one shape are the same statement.
Shape = tuple[frozenset[str], frozenset[str], int, int, int, str]


@dataclass
class ParsedQuery:
    """A statement resolved against the catalog. `texts` are the distinct texts folded into it,
    the first standing for them all; the engine counts their sightings."""

    texts: list[str]
    table_ids: frozenset[str]
    column_ids: frozenset[str]
    nodes_count: int
    join_count: int
    union_count: int
    normalized: str | None  # None when no dialect normalizes it: it then matches only its text
    joins: list[Link] = field(default_factory=list[Link])
    unions: list[Link] = field(default_factory=list[Link])

    @property
    def sql_text(self) -> str:
        return self.texts[0]

    @property
    def prefilter(self) -> Prefilter:
        return (
            self.table_ids,
            self.column_ids,
            self.nodes_count,
            self.join_count,
            self.union_count,
        )

    @property
    def shape(self) -> Shape | None:
        return None if self.normalized is None else (*self.prefilter, self.normalized)


@dataclass
class ChunkResult:
    parsed: list[ParsedQuery]
    unmatched: int = 0  # statements that touch no table the catalog holds
    failed: int = 0  # statements that raised
    failures: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # text, error


def normalize(sql: str, dialects: Sequence[str]) -> str | None:
    """Upstream's `_normalize_sql`: the first dialect's normalization that succeeds."""
    if not dialects:
        return normalize_sql(sql, dialect=None, ignore_literals=True)
    for d in dialects:
        norm = normalize_sql(sql, dialect=d, ignore_literals=True)
        if norm is not None:
            return norm
    return None


def normalize_many(sqls: Sequence[str], dialects: Sequence[str]) -> list[str | None]:
    return [normalize(sql, dialects) for sql in sqls]


def parse_query(sql: str, catalog: CatalogIndex, dialects: Sequence[str]) -> ParsedQuery | None:
    """Upstream's `parse_query_slim`: the statement resolved against `catalog`, or None when it
    touches no table the catalog holds."""
    extraction = extract_tables_and_columns(sql=sql, dialects=list(dialects), catalog=catalog)
    if not extraction.tables:
        return None

    resolved: dict[str, TableEntry] = {}
    column_ids: set[str] = set()
    for table_key, match in extraction.tables.items():
        table = catalog.table(match.schema_name, table_key.split(".")[-1])
        if table is None:
            continue
        resolved[table_key] = table
        column_ids.update(table.columns[c] for c in match.columns if c in table.columns)
    if not resolved:
        return None

    def link(p: JoinPair | UnionPair, op: str) -> Link | None:
        """Both columns must be in the catalog, as upstream's `is_column_in_table` checks."""
        left, right = resolved.get(p.left_table), resolved.get(p.right_table)
        if left is None or right is None:
            return None
        if p.left_column not in left.columns or p.right_column not in right.columns:
            return None
        lb, rb = p.left_table.split(".")[-1], p.right_table.split(".")[-1]
        return Link(
            left.columns[p.left_column],
            right.columns[p.right_column],
            f"{lb}.{p.left_column} {op} {rb}.{p.right_column}",
        )

    return ParsedQuery(
        texts=[sql],
        table_ids=frozenset(t.id for t in resolved.values()),
        column_ids=frozenset(column_ids),
        nodes_count=extraction.ast_node_count,
        join_count=len(extraction.joins),
        union_count=len(extraction.unions),
        normalized=normalize(sql, dialects),
        joins=[x for p in extraction.joins if (x := link(p, p.operator))],
        unions=[x for p in extraction.unions if (x := link(p, "|"))],
    )


def parse_chunk(
    texts: Sequence[str], catalog: CatalogIndex, dialects: Sequence[str]
) -> ChunkResult:
    """Parse distinct statement texts and fold the ones with one shape together, such as the same
    query with other literals or its SELECT list in another order: the parse pool's task."""
    result = ChunkResult(parsed=[])
    by_shape: dict[Shape, ParsedQuery] = {}
    for text in texts:
        try:
            parsed = parse_query(text, catalog, dialects)
        except Exception as err:
            result.failed += 1
            if len(result.failures) < MAX_REPORTED_FAILURES:
                result.failures.append((text[:200], f"{type(err).__name__}: {err}"))
            continue
        if parsed is None:
            result.unmatched += 1
            continue
        shape = parsed.shape
        if shape is not None and shape in by_shape:
            by_shape[shape].texts.append(text)
            continue
        if shape is not None:
            by_shape[shape] = parsed
        result.parsed.append(parsed)
    return result
