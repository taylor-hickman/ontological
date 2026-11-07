# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's
# auto_ontology/retrieval/text_to_sql/db_probe/jsonb_path_check.py at 2a24f5d, for check 6 of the
# answer gate (ARCHITECTURE.md §8.2). Upstream's check is Postgres-only; this one also checks
# DuckDB, every v1 source's dialect, by probing with `json_keys` and `json_type` in place of
# `jsonb_object_keys` and `jsonb_typeof`. On DuckDB a text column can hold JSON (a Postgres or
# MySQL source's JSON reaches DuckDB as VARCHAR), so only a type that is neither JSON nor text is
# the wrong type. Probe SQL is built through sqlglot, so a key is quoted as a literal, and the
# keys found are sorted, so the message is the same each time. The repair message says JSON where
# upstream's says JSONB. `try_self_apply_fixes` isn't ported: the gate reports issues, and the
# model rewrites its own SQL.

"""JSON key-path check (check 6).

The ingestion-time sample-value hint shown to the model is only a suggestion, and the model can
still write a plausible-looking sibling key (``cost->'fees'->>'repair_estimate'`` when the real
key lives under ``cost->'repair_costs'``). Such a path is valid SQL but returns ``NULL`` for every
row instead of erroring, so it needs its own live check: a bad *column* name is caught by check
2, but a bad *JSON key* is not.

The table/column identifiers are taken from the SQL's own AST, and the real key set is found at
runtime by probing the source.

Only the shapes the ingestion-time sample hint covers are checked: ``col ->> 'key'`` (flat) and
``col -> 'container' ->> 'key'`` (one level of nesting), and their function forms. Deeper chains
are left unchecked rather than guessed at.
"""

# sqlglot's expression tree is loosely typed.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportPrivateImportUsage=false, reportArgumentType=false

from __future__ import annotations

import logging
from typing import Any

import sqlglot
from sqlglot import exp

from ontological.answer.literal_check import (
    _as_column,  # pyright: ignore[reportPrivateUsage]
    _candidate_tables,  # pyright: ignore[reportPrivateUsage]
    _first_value,  # pyright: ignore[reportPrivateUsage]
    _table_nodes,  # pyright: ignore[reportPrivateUsage]
)
from ontological.answer.probe import ProbeExecutor

logger = logging.getLogger(__name__)

DIALECTS = ("duckdb", "postgres")
_TEXT_TYPES = ("VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR")


def _single_json_key(path: Any) -> str | None:
    """The lone key name of a ``JSONPath`` like ``$.foo``, or ``None``.

    Returns ``None`` for anything more complex (wildcards, array indices,
    multi-segment paths) so those are left unchecked rather than misread.
    """
    if not isinstance(path, exp.JSONPath):
        return None
    parts = [e for e in path.expressions if not isinstance(e, exp.JSONPathRoot)]
    if len(parts) != 1:
        return None
    key_node = parts[0]
    if isinstance(key_node, exp.JSONPathKey):
        return str(key_node.this)
    return None


def _parse_extract(
    extract: exp.JSONExtractScalar,
) -> tuple[exp.Column, str | None, str] | None:
    """Decompose a ``->>`` node into ``(column, container_or_None, key)``.

    Only the flat (``col ->> 'key'``) and single-nesting (``col -> 'c' ->>
    'key'``) shapes resolve to a result; anything else (function calls, array
    access, deeper chains) returns ``None`` and is skipped.
    """
    key = _single_json_key(extract.expression)
    if key is None:
        return None

    inner = extract.this
    if isinstance(inner, exp.Column):
        return inner, None, key
    if isinstance(inner, exp.JSONExtract):
        container = _single_json_key(inner.expression)
        col = _as_column(inner.this)
        if container is None or col is None:
            return None
        return col, container, key
    return None


def _keys_probe(table_node: exp.Table, col: exp.Column, container: str | None, dialect: str) -> str:
    """The SQL that lists the keys of the JSON objects under ``col`` (or ``col->container``)."""
    bare = table_node.copy()
    bare.set("alias", None)
    target: exp.Expression = exp.column(col.this.copy())
    if container is not None:
        path = exp.JSONPath(expressions=[exp.JSONPathRoot(), exp.JSONPathKey(this=container)])
        target = exp.JSONExtract(this=target, expression=path)
    t = target.sql(dialect=dialect)
    table_ref = bare.sql(dialect=dialect)
    if dialect == "postgres":
        return (
            f"SELECT DISTINCT jsonb_object_keys({t}) AS value FROM {table_ref} "
            f"WHERE {t} IS NOT NULL AND jsonb_typeof({t}) = 'object'"
        )
    # A text column may hold rows that aren't JSON at all; json_valid keeps json_type off them.
    return (
        f"SELECT DISTINCT unnest(json_keys({t})) AS value FROM {table_ref} "
        f"WHERE CASE WHEN json_valid({t}) THEN json_type({t}) = 'OBJECT' ELSE false END"
    )


def _fetch_json_keys(
    executor: ProbeExecutor,
    table_node: exp.Table,
    col: exp.Column,
    container: str | None,
    dialect: str,
) -> list[str] | None:
    """Real keys present under ``col`` (or ``col->container``) via a live probe.

    Returns ``None`` when the column isn't in this table, the value there is
    never a JSON object, or the probe fails — i.e. "not resolvable here", not
    "the key is missing". Mirrors ``literal_check._fetch_distinct``.
    """
    sql = _keys_probe(table_node, col, container, dialect)
    res = executor.run(sql, purpose="json_path_check_keys")
    if not res["ok"] or not res["rows"]:
        return None
    values = [_first_value(r) for r in res["rows"]]
    return sorted(str(v) for v in values if v is not None)


def _is_wrong_type(data_type: str, dialect: str) -> bool:
    """Whether a column of `data_type` can't be what JSON operators navigate."""
    upper = data_type.upper()
    if "JSON" in upper:
        return False
    return dialect == "postgres" or not upper.startswith(_TEXT_TYPES)


def find_json_path_mismatches(
    executor: ProbeExecutor,
    dialect: str | None,
    sql: str,
    known_types: dict[tuple[str, str], str] | None = None,
) -> list[dict[str, Any]]:
    """Return JSON key paths that don't exist in the live database, or that
    were used on a column known not to be JSON at all.

    ``known_types`` is an optional ``{(table_name.lower(), col_name.lower()):
    data_type}`` map, from the catalog. It's used only as a fallback: when the
    live probe can't confirm real keys for a path, and the column's type can't
    hold JSON, that is surfaced as its own mismatch kind (``"wrong_type"`` set)
    — the model used ``->``/``->>`` on a column that was never JSON, not merely
    a wrong key.

    Each entry: ``{"table", "column", "container", "used_key",
    "available_keys", "sibling_containers"}`` for a real key mismatch, or
    ``{"table", "column", "container", "used_key", "wrong_type": <data_type>}``
    for the non-JSON-column case. Empty list means every checked path resolved,
    or none of the paths in the SQL were checkable (unsupported dialect/shape,
    probe budget exhausted, etc.) — never treated as "everything is fine"
    beyond that.
    """
    if dialect not in DIALECTS:
        return []

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # never break validation on a parse error
        logger.info("json_path_check: could not parse SQL (%s)", exc)
        return []

    all_nodes, by_key = _table_nodes(tree)
    if not all_nodes:
        return []

    mismatches: list[dict[str, Any]] = []
    keys_cache: dict[tuple[str, str, str | None], list[str] | None] = {}

    for extract in tree.find_all(exp.JSONExtractScalar):
        parsed = _parse_extract(extract)
        if parsed is None:
            continue
        col, container, key = parsed

        candidates = _candidate_tables(col, all_nodes, by_key)
        available: list[str] | None = None
        owning_table: str | None = None
        for node in candidates:
            if not executor.budget_left:
                break
            cache_key = (node.name.lower(), col.name.lower(), container)
            if cache_key not in keys_cache:
                keys_cache[cache_key] = _fetch_json_keys(executor, node, col, container, dialect)
            if keys_cache[cache_key] is not None:
                available = keys_cache[cache_key]
                owning_table = node.name
                break
        if available is None or owning_table is None:
            # Not resolvable, never an object, or the probe failed. If the column's real type
            # can't hold JSON, the probe's silence means the operator is misused.
            if known_types is not None:
                for node in candidates:
                    known_type = known_types.get((node.name.lower(), col.name.lower()))
                    if known_type and _is_wrong_type(known_type, dialect):
                        mismatches.append(
                            {
                                "table": node.name,
                                "column": col.name,
                                "container": container,
                                "used_key": key,
                                "wrong_type": known_type,
                            }
                        )
                        break
            continue

        if key not in available:
            sibling_containers: list[str] | None = None
            if container is not None:
                # Also surface the column's own top-level sections (siblings
                # of the wrong container) so the model has somewhere to
                # look next, not just proof that the guessed spot was wrong.
                top_cache_key = (owning_table.lower(), col.name.lower(), None)
                if top_cache_key not in keys_cache:
                    node = by_key.get(owning_table.lower())
                    keys_cache[top_cache_key] = (
                        _fetch_json_keys(executor, node, col, None, dialect)
                        if node is not None and executor.budget_left
                        else None
                    )
                sibling_containers = keys_cache[top_cache_key]

            mismatches.append(
                {
                    "table": owning_table,
                    "column": col.name,
                    "container": container,
                    "used_key": key,
                    "available_keys": available,
                    "sibling_containers": sibling_containers,
                }
            )

    return mismatches


def _path(m: dict[str, Any]) -> str:
    if m["container"]:
        return f"{m['column']}->'{m['container']}'->>'{m['used_key']}'"
    return f"{m['column']}->>'{m['used_key']}'"


def build_json_path_repair_error(mismatches: list[dict[str, Any]]) -> str:
    """Render mismatches into a targeted repair instruction."""
    key_mismatches = [m for m in mismatches if "wrong_type" not in m]
    type_mismatches = [m for m in mismatches if "wrong_type" in m]
    sections: list[str] = []

    if key_mismatches:
        lines: list[str] = []
        for m in key_mismatches:
            available_preview = ", ".join(m["available_keys"][:20])
            line = (
                f"- {m['table']}.{_path(m)}: this key does not exist and returns NULL "
                f"for every row. Actual keys available there: [{available_preview}]."
            )
            siblings = m.get("sibling_containers")
            if siblings:
                sibling_preview = ", ".join(siblings[:20])
                line += (
                    f" Other top-level sections in {m['column']}: [{sibling_preview}] — "
                    "the key you want may live under one of these instead."
                )
            lines.append(line)
        sections.append(
            "One or more JSON key paths in the generated SQL do not exist in the "
            "database, so they return NULL for every row instead of erroring:\n"
            + "\n".join(lines)
            + "\n\nRewrite the SQL using the correct container/key name from the actual "
            "keys listed above. Change ONLY the mismatched JSON path(s); keep all "
            "joins, columns, grouping, and other filters exactly as they are."
        )

    if type_mismatches:
        lines = [
            f"- {m['table']}.{_path(m)}: {m['table']}.{m['column']} is a "
            f"{m['wrong_type']} column, not JSON — this is not a wrong key, "
            "the ->/->> operators don't apply to this column at all."
            for m in type_mismatches
        ]
        sections.append(
            "One or more JSON navigation operators were used on a column that "
            "isn't JSON-typed:\n"
            + "\n".join(lines)
            + "\n\nRewrite the SQL to reference the column directly (no ->/->> "
            "operators) instead of treating it as JSON. Change ONLY the "
            "affected reference(s); keep all joins, columns, grouping, and "
            "other filters exactly as they are."
        )

    return "\n\n".join(sections)


__all__ = ["build_json_path_repair_error", "find_json_path_mismatches"]
