"""Upstream's retrieval filters, for the rankings that don't go through langchain-postgres
(ARCHITECTURE.md §7.3).

A filter is the dict `PostgresVDB.retrieval` passes to langchain-postgres as `where`, over the
VDB's two metadata columns, `label` and `database_name`:

    {"label": "CustomAnalysis"}                              equality
    {"label": ["Term", "ColumnAttribute"]}                   one of (upstream's flat list form)
    {"database_name": {"$eq" | "$ne" | "$in" | "$nin": v}}   an operator
    {"$and": [...]}, {"$or": [...]}                          combinations

Fields side by side are ANDed. It compiles to SQL for the dense and trigram rankings, and to a
mask over the BM25 index's columns, with SQL's NULL rules in both: a row whose column is NULL
matches no condition on it, `$ne` and `$nin` included. None is refused as a value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray
from psycopg import sql

COLUMNS = ("label", "database_name")
OPERATORS = ("$eq", "$ne", "$in", "$nin")

Mask = NDArray[np.bool_]


@dataclass(frozen=True)
class Cond:
    column: str
    op: str  # one of OPERATORS
    values: tuple[str, ...]


@dataclass(frozen=True)
class Group:
    op: str  # "$and" | "$or"
    parts: tuple[Filter, ...]


type Filter = Cond | Group

EVERYTHING: Filter = Group("$and", ())


def parse(where: Mapping[str, Any] | None) -> Filter:
    """The filter `where` describes; None or {} is everything. Raises ValueError for a column or
    operator upstream's filters don't use, so nothing is quietly left unfiltered."""
    parts: list[Filter] = []
    for key, value in (where or {}).items():
        if key in ("$and", "$or"):
            if not isinstance(value, list | tuple):
                raise ValueError(f"{key} takes a list of filters, got {value!r}")
            items = cast(Sequence[Mapping[str, Any]], value)
            parts.append(Group(key, tuple(parse(item) for item in items)))
        elif key in COLUMNS:
            parts.append(_cond(key, value))
        else:
            raise ValueError(f"cannot filter on {key!r}: only {', '.join(COLUMNS)}")
    if not parts:
        return EVERYTHING
    return parts[0] if len(parts) == 1 else Group("$and", tuple(parts))


def _cond(column: str, value: Any) -> Cond:
    operand: Any
    if isinstance(value, Mapping):
        if len(cast(Mapping[str, Any], value)) != 1:
            raise ValueError(f"{column}: one operator per condition, got {value!r}")
        [(op, operand)] = cast(Mapping[str, Any], value).items()
        if op not in OPERATORS:
            raise ValueError(f"{column}: unsupported operator {op!r}")
    elif isinstance(value, list | tuple):
        op, operand = "$in", cast(Sequence[Any], value)
    else:
        op, operand = "$eq", value
    many = op in ("$in", "$nin")
    if many != isinstance(operand, list | tuple):
        raise ValueError(f"{column}: {op} takes {'a list' if many else 'one value'}")
    values: Sequence[Any] = cast(Sequence[Any], operand) if many else [operand]
    if any(v is None for v in values):
        # SQL's `= NULL` and `NOT IN (NULL, ...)` match nothing, which is never what was meant.
        raise ValueError(f"{column}: None is not a value to filter on")
    return Cond(column, op, tuple(str(v) for v in values))


# --- SQL ---------------------------------------------------------------------------------------


def to_sql(f: Filter) -> sql.Composable:
    """A boolean SQL expression over the VDB table's columns, with its values bound."""
    if isinstance(f, Group):
        if not f.parts:
            return sql.SQL("TRUE")
        joiner = sql.SQL(" AND " if f.op == "$and" else " OR ")
        return sql.SQL("({})").format(joiner.join(to_sql(p) for p in f.parts))
    column = sql.Identifier(f.column)
    values = list(f.values)
    match f.op:
        case "$eq":
            return sql.SQL("{} = {}").format(column, sql.Literal(values[0]))
        case "$ne":
            return sql.SQL("{} <> {}").format(column, sql.Literal(values[0]))
        case "$in":
            return sql.SQL("{} = ANY({}::text[])").format(column, sql.Literal(values))
        case _:  # $nin
            return sql.SQL("({c} IS NOT NULL AND NOT ({c} = ANY({v}::text[])))").format(
                c=column, v=sql.Literal(values)
            )


# --- masks -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    """One metadata column of an index's documents, as codes into its distinct values; -1 is
    NULL."""

    codes: NDArray[np.int32]
    index: Mapping[str, int]

    @classmethod
    def of(cls, values: Sequence[str | None]) -> Column:
        index: dict[str, int] = {}
        codes = np.fromiter(
            (-1 if v is None else index.setdefault(v, len(index)) for v in values),
            dtype=np.int32,
            count=len(values),
        )
        return cls(codes, index)

    def isin(self, values: Sequence[str]) -> Mask:
        wanted = [self.index[v] for v in values if v in self.index]
        return np.isin(self.codes, wanted)


def mask(f: Filter, columns: Mapping[str, Column], n: int) -> Mask:
    """Which of `n` documents `f` keeps."""
    if isinstance(f, Group):
        if not f.parts:
            return np.ones(n, dtype=np.bool_)
        masks = [mask(p, columns, n) for p in f.parts]
        reduce = np.logical_and if f.op == "$and" else np.logical_or
        return reduce.reduce(masks)
    column = columns[f.column]
    hit = column.isin(f.values)
    if f.op in ("$eq", "$in"):
        return hit
    return ~hit & (column.codes != -1)
