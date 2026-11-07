# sqlglot's expression tree is loosely typed.
# pyright: reportPrivateImportUsage=false

"""Empty-result handling's LIKE step (ARCHITECTURE.md §8.1): when `run_sql` comes back empty and
the SQL filters with LIKE or ILIKE, the same query runs again as a probe with those filters
dropped. Rows coming back mean the LIKE filters emptied the result.

Upstream asks a model which LIKE predicates are non-essential and has the SQL rewritten without
them (`agents/empty_like_result_check.py` at 2a24f5d). Here the query is probed without all of
them, through the question's `ProbeExecutor`, so the probe shares its cap, and the model reads
what came back and decides which filter to fix.

A filter is dropped by putting TRUE in its place, so only a filter whose way up to its WHERE,
HAVING, QUALIFY or JOIN ON clause passes through AND, OR and parentheses alone is dropped: there,
TRUE can only let more rows through. Under a NOT, or inside a CASE or a function, it could keep
rows out instead, so such a filter stays."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from ontological.answer.probe import ProbeExecutor

SAMPLE_ROWS = 5  # the rows of the probe the model is shown

_CLAUSES = (exp.Where, exp.Having, exp.Qualify, exp.Join)
_CONNECTIVES = (exp.And, exp.Or, exp.Paren)


@dataclass(frozen=True)
class WithoutLike:
    """What the query returned without its LIKE filters."""

    dropped: list[str]  # the filters, as the source's dialect writes them
    sql: str  # the query that ran without them
    columns: list[str]
    rows: list[list[Any]]  # the first SAMPLE_ROWS
    truncated: bool  # it returned more than SAMPLE_ROWS


def drop_like_filters(sql: str, dialect: str) -> tuple[str, list[str]] | None:
    """`sql` with each LIKE and ILIKE filter it can drop replaced by TRUE, and the filters
    dropped; None when there's none, or the SQL doesn't parse."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (ParseError, TokenError):
        return None
    filters = [node for like in tree.find_all(exp.Like, exp.ILike) if (node := _droppable(like))]
    if not filters:
        return None
    dropped = [node.sql(dialect=dialect) for node in filters]
    for node in filters:
        node.replace(exp.true())
    return tree.sql(dialect=dialect), dropped


def probe_without_like(probes: ProbeExecutor, dialect: str, sql: str) -> WithoutLike | None:
    """Runs `sql` without its LIKE filters as one probe. What it returned, or None when it has
    no filter to drop, the probe failed (the cap, a timeout, an error) or it came back empty
    too."""
    relaxed = drop_like_filters(sql, dialect)
    if relaxed is None:
        return None
    relaxed_sql, dropped = relaxed
    entry = probes.run(relaxed_sql, purpose="the empty query without its LIKE filters")
    if not entry["ok"] or not entry["row_count"]:
        return None
    rows: list[dict[str, Any]] = entry["rows"]
    columns = list(rows[0])
    return WithoutLike(
        dropped=dropped,
        sql=relaxed_sql,
        columns=columns,
        rows=[[row[c] for c in columns] for row in rows[:SAMPLE_ROWS]],
        truncated=entry["truncated"] or len(rows) > SAMPLE_ROWS,
    )


def _droppable(like: exp.Expression) -> exp.Expression | None:
    """The filter `like` heads, with its ESCAPE, when TRUE in its place can only let rows
    through; otherwise None. Up through AND, OR and parentheses alone, a filter can only reach
    a JOIN from its ON."""
    node = like.parent if isinstance(like.parent, exp.Escape) else like
    parent = node.parent
    while isinstance(parent, _CONNECTIVES):
        parent = parent.parent
    return node if isinstance(parent, _CLAUSES) else None
