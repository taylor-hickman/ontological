# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: the sqlglot half of upstream's
# auto_ontology/semantic/sql_attribute_suggester.py at 2a24f5d (`_extract_expressions`,
# `_try_wrap_in_select`, `_qualify_table_names`, `_build_valid_sql`), with types and the imports
# at the top. The rules are upstream's. Extraction runs in the parse pool (`extract_many`).

"""SQL fragments from query history, and the SELECTs that wrap them (ARCHITECTURE.md §1.3).

`extract_expressions` pulls the parts of a statement that can carry business logic: WHERE and
JOIN ON comparisons, aggregates, functions and CASEs, HAVING, and set operations with their
branches. `build_valid_sql` turns a fragment back into a statement that runs: a filter becomes
`SELECT * FROM … WHERE <fragment>`, an aggregate `SELECT <fragment> FROM …`, with the FROM and
the JOINs the fragment needs taken from a statement it came from.
"""

# sqlglot's AST args are untyped, and its own annotations mix `Expr` and `Expression`, as in
# `catalog/parsers/sqlglot_extractor.py`.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportPrivateImportUsage=false
# pyright: reportArgumentType=false, reportReturnType=false, reportUnnecessaryComparison=false

from __future__ import annotations

from collections.abc import Mapping, Sequence

import sqlglot
import sqlglot.expressions as exp

_BARE_KEYWORDS = frozenset(
    {"select", "where", "from", "join", "on", "union", "intersect", "except", "having"}
)

_COMPARISONS = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.In,
    exp.Between,
    exp.Like,
    exp.Is,
)


def _parse(sql: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(sql, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        return None


def _is_pure_fk_eq(node: exp.Expression) -> bool:
    """A bare `col = col` equality, with no literal on either side."""
    return (
        isinstance(node, exp.EQ)
        and isinstance(node.left, exp.Column)
        and isinstance(node.right, exp.Column)
    )


def extract_expressions(sql_text: str) -> list[str]:
    """The statement's distinct sub-expressions worth judging, in upstream's order.

    Kept: WHERE and JOIN ON comparisons; aggregates, anonymous functions and CASEs anywhere;
    the HAVING predicate; each whole JOIN ON predicate; each set operation and its branches.
    Left out: bare columns, stars, literals, `col = col` key equalities, keyword-only emissions,
    and anything shorter than five characters."""
    ast = _parse(sql_text)
    if ast is None:
        return []

    seen: set[str] = set()
    results: list[str] = []

    def add(node: exp.Expression) -> None:
        text = node.sql().strip()
        key = text.lower()
        if len(text) < 5 or key in seen:
            return
        if isinstance(node, (exp.Column, exp.Star, exp.Literal)):
            return
        if key in _BARE_KEYWORDS or _is_pure_fk_eq(node):
            return
        seen.add(key)
        results.append(text)

    try:
        where = ast.find(exp.Where)
        if where:
            for node in where.walk():
                if isinstance(node, _COMPARISONS):
                    add(node)

        for node in ast.walk():
            if isinstance(node, (exp.AggFunc, exp.Anonymous, exp.Case)):
                add(node)

        having = ast.find(exp.Having)
        if having and having.this:
            add(having.this)

        for join in ast.find_all(exp.Join):
            on = join.args.get("on")
            if on:
                add(on)
                for node in on.walk():
                    if isinstance(node, _COMPARISONS):
                        add(node)

        for set_op in ast.find_all(exp.Union, exp.Intersect, exp.Except):
            add(set_op)
            for branch in (set_op.left, set_op.right):
                if branch is not None:
                    add(branch)
    except Exception:
        return results
    return results


def extract_many(texts: Sequence[str]) -> list[list[str]]:
    """`extract_expressions` of each text, in order: a parse-pool task."""
    return [extract_expressions(t) for t in texts]


def _table_key(table: exp.Table) -> str:
    return (table.alias or table.name).lower()


def _try_wrap_in_select(
    expression: str, source_sql: str, expr_ast: exp.Expression | None, is_agg: bool
) -> str | None:
    """`SELECT … FROM … [JOINs] [WHERE expression]` from `source_sql`'s first SELECT whose tables
    cover the aliases `expression` names, or else its first SELECT with a FROM. Only the JOINs
    whose tables the expression names are kept. None when nothing fits."""
    src_ast = _parse(source_sql)
    if src_ast is None:
        return None

    aliases: set[str] = set()
    if expr_ast is not None:
        aliases = {col.table.lower() for col in expr_ast.find_all(exp.Column) if col.table}

    try:
        best: exp.Select | None = None
        for sel in src_ast.find_all(exp.Select):
            if sel.args.get("from_") is None:
                continue
            covered: set[str] = set()
            for tbl in sel.find_all(exp.Table):
                if tbl.alias:
                    covered.add(tbl.alias.lower())
                covered.add(tbl.name.lower())
            if not aliases or aliases.issubset(covered):
                best = sel
                break
        if best is None:
            best = next((s for s in src_ast.find_all(exp.Select) if s.args.get("from_")), None)
        if best is None:
            return None

        from_clause = best.args.get("from_")
        if from_clause is None:
            return None
        joins: list[exp.Join] = best.args.get("joins") or []
        kept = (
            [j for j in joins if any(_table_key(t) in aliases for t in j.find_all(exp.Table))]
            if aliases
            else []
        )
        from_str = from_clause.sql()
        joins_str = " ".join(j.sql() for j in kept)
    except Exception:
        return None

    parts = ["SELECT", expression if is_agg else "*", from_str]
    if joins_str:
        parts.append(joins_str)
    if not is_agg:
        parts.append(f"WHERE {expression}")
    return " ".join(parts)


def qualify_table_names(sql: str, schema_map: Mapping[str, str]) -> str:
    """`sql` with `schema.` put before each bare table name `schema_map` (lower-cased table name
    to schema) knows. Unchanged when the map is empty or the SQL doesn't parse."""
    if not schema_map:
        return sql
    ast = _parse(sql)
    if ast is None:
        return sql

    def add_schema(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and not node.args.get("db") and not node.args.get("catalog"):
            schema = schema_map.get(node.name.lower())
            if schema:
                n = node.copy()
                n.set("db", exp.to_identifier(schema))
                return n
        return node

    try:
        return ast.transform(add_schema).sql()
    except Exception:
        return sql


class NoContext(ValueError):
    """No source statement gives a fragment the FROM it needs."""


def build_valid_sql(
    expression: str,
    source_sql_texts: Sequence[str],
    schema_map: Mapping[str, str] | None = None,
) -> str:
    """A statement that runs `expression`, its table names schema-qualified from `schema_map`.

    A full SELECT or set operation is kept as it is. A filter or join condition becomes
    `SELECT * FROM <from + joins> WHERE <expression>`, and an aggregate or function
    `SELECT <expression> FROM <from + joins>`, with the FROM taken from the first source
    statement that shares a word with the expression, else from the first that has one. Raises
    `NoContext` when none does."""
    schema_map = schema_map or {}
    ast = _parse(expression)
    if isinstance(ast, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return qualify_table_names(expression, schema_map)
    is_agg = isinstance(ast, (exp.AggFunc, exp.Anonymous))

    words = [tok for tok in expression.lower().split() if len(tok) > 3]
    for sql_text in source_sql_texts:
        if not sql_text or not any(tok in sql_text.lower() for tok in words):
            continue
        wrapped = _try_wrap_in_select(expression, sql_text, ast, is_agg)
        if wrapped:
            return qualify_table_names(wrapped, schema_map)
    for sql_text in source_sql_texts:
        if not sql_text:
            continue
        wrapped = _try_wrap_in_select(expression, sql_text, ast, is_agg)
        if wrapped:
            return qualify_table_names(wrapped, schema_map)
    raise NoContext(
        f"Cannot build a valid SELECT for expression {expression!r}: "
        "no source SQL context available."
    )
