# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: check 3 is ported from upstream's
# auto_ontology/retrieval/text_to_sql/agents/sql_parse_validation.py at 2a24f5d
# (`detect_degenerate_sql`, `detect_missing_aggregation` and their helpers, with upstream's
# messages), reading key columns through a callable in place of the store. Checks 1 and 2 are
# Ontological's: upstream runs only what its graph generated, and resolves names with
# `parse_query_single`, which drops a table or column it can't find instead of naming it.
# Upstream's vacuous GROUP BY check isn't ported: upstream ships it off
# (`DETECT_VACUOUS_GROUP_BY`), because it misfires on incomplete key metadata.

"""`validate_sql`'s gate, checks 1 to 3 (ARCHITECTURE.md §8.2). All deterministic; none probes
the source.

1. The SQL parses in the source's dialect (DuckDB for every v1 source, §5.5) as exactly one
   SELECT, UNION or WITH query. Anything else, DML, DDL, ATTACH, COPY, PRAGMA, INSTALL and LOAD
   among it, is rejected. The executor refuses those too; the gate says why first.
2. Every table and column resolves against the database's catalog.
3. No degenerate SQL (`SELECT NULL`, `WHERE 1=0`, `LIMIT 0`), and no many-side column selected
   raw under a GROUP BY.

Checks 4 to 7 (join paths, literals, JSON paths, dialect rewrites) are M4.1's.
"""

# sqlglot's expression tree is loosely typed.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportPrivateImportUsage=false, reportArgumentType=false, reportReturnType=false

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from ontological.catalog.parsers.catalog_index import CatalogIndex, TableEntry

DIALECT = "duckdb"


@dataclass(frozen=True)
class KeyColumns:
    """A table's primary-key columns and its columns profiling found unique, lower-cased
    (upstream's `find_table_key_columns`)."""

    pk: list[str] = field(default_factory=list[str])
    unique: list[str] = field(default_factory=list[str])


# A table's key columns, by its bare name.
KeyLookup = Callable[[str], KeyColumns]


@dataclass(frozen=True)
class GateResult:
    issues: list[str]
    tables: list[str] = field(default_factory=list[str])  # schema.Table, as the catalog spells it
    columns: list[str] = field(default_factory=list[str])  # Table.Column

    @property
    def ok(self) -> bool:
        return not self.issues


def check_sql(
    sql: str, catalog: CatalogIndex, *, key_columns: KeyLookup, dialect: str = DIALECT
) -> GateResult:
    """Checks 1 to 3, in order. The first check that fails gives the issues."""
    statement = _one_query(sql, dialect)
    if isinstance(statement, str):
        return GateResult([statement])
    resolved = _resolve(statement, catalog, dialect)
    if resolved.issues:
        return resolved
    for reason in (
        _degenerate(statement),
        detect_missing_aggregation(statement, key_columns),
    ):
        if reason:
            return GateResult([reason], resolved.tables, resolved.columns)
    return resolved


# --- check 1 ---------------------------------------------------------------------------------


def _one_query(sql: str, dialect: str) -> exp.Expression | str:
    """The one query `sql` holds, or the issue with it."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except (ParseError, TokenError) as e:
        return f"The SQL doesn't parse as DuckDB SQL: {e}"
    if not statements:
        return "The SQL is empty."
    if len(statements) > 1:
        return f"The SQL holds {len(statements)} statements; send exactly one statement."
    [statement] = statements
    if not isinstance(statement, exp.Query) or any(
        isinstance(node, _WRITES) for node in statement.walk()
    ):
        return (
            f"This is a {type(statement).__name__.upper()} statement; only a single SELECT "
            "(or WITH ... SELECT, or a UNION of SELECTs) can run on a source."
        )
    return statement


_WRITES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Attach,
    exp.Detach,
    exp.Pragma,
    exp.Install,
    exp.Set,
)


# --- check 2 ---------------------------------------------------------------------------------


def _resolve(statement: exp.Expression, catalog: CatalogIndex, dialect: str) -> GateResult:
    """Every table in `catalog`, then every column in its table. The tables are matched first,
    because sqlglot's `qualify` lets a table it doesn't know through and then fails on its
    columns; `qualify` then resolves the columns against the catalog's."""
    issues: list[str] = []
    tables: dict[str, str] = {}
    for scope in traverse_scope(statement):
        for source in _catalog_sources(scope):
            if source.catalog:
                issues.append(
                    f"{source.sql(dialect=dialect)} names a database ({source.catalog}); "
                    "write schema.table or the table alone."
                )
                continue
            entry = catalog.table(source.db.lower() or None, source.name.lower())
            if entry is None:
                issues.append(_unknown_table(source, catalog))
            else:
                tables[entry.id] = f"{_schema_of(entry, catalog)}.{entry.name}"
    if issues:
        return GateResult(list(dict.fromkeys(issues)))
    if not tables:
        return GateResult(["The SQL reads no table from the catalog."])

    schema = {
        name: {t: dict.fromkeys(entry.columns, "TEXT") for t, entry in s.tables.items()}
        for name, s in catalog.schemas.items()
    }
    try:
        qualified = qualify(
            statement.copy(),
            schema=schema,
            dialect=dialect,
            validate_qualify_columns=True,
            quote_identifiers=False,
            identify=False,
        )
    except OptimizeError as e:
        return GateResult([f"{e}. {_known_columns_hint(statement, catalog)}".strip()])

    columns: set[str] = set()
    for scope in traverse_scope(qualified):
        aliases = {
            alias: entry
            for alias, source in scope.sources.items()
            if isinstance(source, exp.Table)
            and (entry := catalog.table(source.db.lower() or None, source.name.lower()))
        }
        for column in scope.columns:
            entry = aliases.get(column.table)
            if entry is not None and column.name in entry.columns:
                columns.add(f"{entry.name}.{_spelled(entry, column.name)}")
    return GateResult([], sorted(tables.values()), sorted(columns))


def _catalog_sources(scope: Scope) -> list[exp.Table]:
    """The tables a scope reads by name: not its CTEs, subqueries or table functions."""
    return [
        source
        for source in scope.sources.values()
        if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier)
    ]


def _schema_of(entry: TableEntry, catalog: CatalogIndex) -> str:
    return next(name for name, s in catalog.schemas.items() if entry in s.tables.values())


def _spelled(entry: TableEntry, column: str) -> str:
    return entry.spellings.get(column, column)


def _unknown_table(table: exp.Table, catalog: CatalogIndex) -> str:
    names = sorted({t.name for s in catalog.schemas.values() for t in s.tables.values()})
    near = difflib.get_close_matches(table.name, names, n=3, cutoff=0.5)
    hint = f" Did you mean {', '.join(near)}?" if near else f" The tables are {', '.join(names)}."
    return f"Unknown table {table.name}: it isn't in the catalog.{hint}"


def _known_columns_hint(statement: exp.Expression, catalog: CatalogIndex) -> str:
    """The columns of the catalog tables the SQL names, so the model can pick the right one."""
    found: list[str] = []
    for table in statement.find_all(exp.Table):
        entry = catalog.table(table.db.lower() or None, table.name.lower())
        if entry is not None:
            cols = sorted(entry.spellings.values() or entry.columns)
            found.append(f"{entry.name}: {', '.join(cols)}")
    return f"Columns: {'; '.join(dict.fromkeys(found))}." if found else ""


# --- check 3 (upstream's) --------------------------------------------------------------------


def _unwrap_projection(e: exp.Expression) -> exp.Expression:
    """Strip an alias wrapper so ``NULL AS x`` is seen as ``NULL``."""
    return e.this if isinstance(e, exp.Alias) else e


def _is_always_false(cond: exp.Expression | None) -> bool:
    """True for constant-false predicates like ``1=0``, ``0=1``, ``FALSE``."""
    if cond is None:
        return False
    if isinstance(cond, exp.Boolean):
        return cond.this is False
    if isinstance(cond, exp.EQ):
        left, right = cond.left, cond.right
        if (
            isinstance(left, exp.Literal)
            and isinstance(right, exp.Literal)
            and left.is_number
            and right.is_number
        ):
            return left.name != right.name
    return False


def detect_degenerate_sql(statement: exp.Expression) -> str:
    """A reason when the query is a placeholder: constant-only projections, an always-false
    WHERE, LIMIT 0. Only the outermost SELECT is inspected, so `EXISTS (SELECT 1 ...)` is fine."""
    select = statement if isinstance(statement, exp.Select) else statement.find(exp.Select)
    if select is None:
        return ""

    projections = list(select.expressions or [])
    if projections and all(
        isinstance(_unwrap_projection(p), exp.Null | exp.Literal | exp.Boolean) for p in projections
    ):
        return (
            "the query only selects constant/NULL values instead of real data "
            "from the tables (e.g. SELECT NULL)"
        )

    where = select.args.get("where")
    if where is not None and _is_always_false(where.this):
        return (
            "the query has an always-false WHERE condition (e.g. 1=0), so it can "
            "never return any rows"
        )

    limit = select.args.get("limit")
    if limit is not None:
        limit_expr = getattr(limit, "expression", None)
        if isinstance(limit_expr, exp.Literal) and limit_expr.is_number and limit_expr.name == "0":
            return "the query uses LIMIT 0, so it always returns no rows"

    return ""


def _degenerate(statement: exp.Expression) -> str:
    reason = detect_degenerate_sql(statement)
    if not reason:
        return ""
    return (
        f"The generated SQL is a placeholder that does not answer the "
        f"question: {reason}. Rewrite a real query that selects "
        f"the requested data from the available tables. Do NOT use SELECT "
        f"NULL, constant-only projections, always-false conditions such as "
        f"WHERE 1=0, or LIMIT 0."
    )


def _select_scope_table_nodes(
    select: exp.Select,
) -> tuple[list[exp.Table], dict[str, exp.Table]]:
    """Table nodes introduced directly in *select*'s own FROM/JOIN, not a derived subquery's."""
    nodes: list[exp.Table] = []
    from_clause = select.find(exp.From)
    if from_clause is not None and isinstance(from_clause.this, exp.Table):
        nodes.append(from_clause.this)
    for join in select.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            nodes.append(join.this)
    by_key: dict[str, exp.Table] = {}
    for t in nodes:
        if t.name:
            by_key.setdefault(t.name.lower(), t)
        alias = t.alias
        if alias:
            by_key[alias.lower()] = t
    return nodes, by_key


def _resolve_column_table(col: exp.Column, by_key: dict[str, exp.Table]) -> exp.Table | None:
    """The table node *col* belongs to, from its own qualifier only; None when unqualified."""
    qualifier = (col.table or "").lower()
    return by_key.get(qualifier) if qualifier else None


def _many_side_tables(
    select: exp.Select, by_key: dict[str, exp.Table], key_columns: KeyLookup
) -> set[str]:
    """Lower-cased names of the tables on the many side of a `JOIN ... ON` equality, from key
    metadata: an edge counts only when exactly one side is known unique on its table."""
    many: set[str] = set()
    for join in select.args.get("joins") or []:
        on = join.args.get("on")
        if on is None:
            continue
        for eq in on.find_all(exp.EQ):
            left, right = eq.left, eq.right
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            table_left = _resolve_column_table(left, by_key)
            table_right = _resolve_column_table(right, by_key)
            if table_left is None or table_right is None:
                continue
            if table_left.name.lower() == table_right.name.lower():
                continue  # self-join — not this check's job
            keys_left = key_columns(table_left.name)
            keys_right = key_columns(table_right.name)
            unique_left = left.name.lower() in (keys_left.pk + keys_left.unique)
            unique_right = right.name.lower() in (keys_right.pk + keys_right.unique)
            if unique_left and not unique_right:
                many.add(table_right.name.lower())
            elif unique_right and not unique_left:
                many.add(table_left.name.lower())
    return many


def detect_missing_aggregation(statement: exp.Expression, key_columns: KeyLookup) -> str:
    """A reason when a SELECT block groups by one table's columns and selects a many-side
    joined table's column raw, outside any aggregate: it can have more than one value per
    group. Only blocks with an explicit GROUP BY and a JOIN whose cardinality the keys confirm."""
    for select in statement.find_all(exp.Select):
        reason = _check_select_block_missing_agg(select, key_columns)
        if reason:
            return f"The generated SQL's GROUP BY is unsafe: {reason}"
    return ""


def _group_expr_covers(col: exp.Column, group_exprs: list[exp.Expression]) -> bool:
    """True when *col* sits inside a projection subtree that renders the same as a GROUP BY
    entry, such as the same CASE expression selected and grouped by."""
    if not group_exprs:
        return False
    group_sqls = {g.sql() for g in group_exprs}
    ancestor = col.parent
    while ancestor is not None:
        if ancestor.sql() in group_sqls:
            return True
        ancestor = ancestor.parent
    return False


def _check_select_block_missing_agg(select: exp.Select, key_columns: KeyLookup) -> str:
    group = select.args.get("group")
    if group is None or not select.args.get("joins"):
        return ""

    all_nodes, by_key = _select_scope_table_nodes(select)
    if len(all_nodes) < 2:
        return ""

    grouping_pairs: set[tuple[str, str]] = set()
    grouping_tables: set[str] = set()
    group_exprs: list[exp.Expression] = []
    for e in group.expressions or []:
        ge = e.this if isinstance(e, exp.Ordered) else e
        group_exprs.append(ge)
        if not isinstance(ge, exp.Column):
            continue
        table = _resolve_column_table(ge, by_key)
        if table is None:
            continue
        grouping_pairs.add((table.name.lower(), ge.name.lower()))
        grouping_tables.add(table.name.lower())
    if not grouping_tables:
        return ""

    many_tables = _many_side_tables(select, by_key, key_columns) - grouping_tables
    if not many_tables:
        return ""

    for proj in select.expressions or []:
        for col in proj.find_all(exp.Column):
            if col.find_ancestor(exp.AggFunc) is not None:
                continue
            table = _resolve_column_table(col, by_key)
            if table is None:
                continue
            table_name = table.name.lower()
            if table_name not in many_tables:
                continue
            if (table_name, col.name.lower()) in grouping_pairs:
                continue
            if _group_expr_covers(col, group_exprs):
                continue
            anchor_desc = ", ".join(f'"{t}"' for t in sorted(grouping_tables))
            culprit = f"{table.alias_or_name}.{col.name}"
            return (
                f'You grouped by {anchor_desc}, but "{table.name}" is joined '
                f'many-to-one against that key and "{culprit}" is selected raw — '
                f"it can have more than one value per group. Wrap it in the "
                f"aggregate that matches what the question asks for that field "
                f"(e.g. AVG, SUM, MAX), or add it to GROUP BY only if it is truly "
                f"one value per group. Change only {culprit}; keep every other "
                f"join, filter, and column exactly as they are."
            )
    return ""
