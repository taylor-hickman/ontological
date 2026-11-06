# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's `calculate_columns_profiling` in
# auto_ontology/semantic/visit_enter.py at 2a24f5d. It queries through a source executor's `probe`
# in place of a connector, and returns the profile instead of writing it. JSON key paths, the
# mixed-date-shape warnings and the sampling circuit breaker wait for M2.5's full profiling.

"""Column profiling from a live sample (ARCHITECTURE.md §1.3, stages 1 and 2).

`SELECT * ... LIMIT 1000` gives each column an `is_unique` flag and its five most common values.
A non-unique text column with fewer than five values in the sample gets a `SELECT DISTINCT` probe,
so rare enum values the row prefix missed still reach the prompts. Date and text columns get the
storage notation every sampled value shares (`date_format.py`).

The queries run in the source's executor; the statistics are computed here. M2.5 moves them into
the executor too.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, cast

from ontological.profile.date_format import infer_date_format, is_date_type
from ontological.sources.executor import MAX_PROFILE_ROWS, SourceError, SourceExecutor
from ontological.sources.session import quote_ident

logger = logging.getLogger(__name__)

# How long a profiling query may run. A bounded sample is seconds; past this the source isn't
# answering (upstream's _PROFILING_QUERY_TIMEOUT_SECONDS).
PROFILE_TIMEOUT_S = 120.0
TOP_N = 5  # most-common values kept per column
MAX_SAMPLE_VALUE_LEN = 30  # string sample values longer than this are not persisted
LOW_CARDINALITY_MAX = 25  # a text column with at most this many distinct values is categorical
# Declared type substrings whose sample values are not persisted.
EXCLUDED_SAMPLE_TYPES = ("date", "time", "timestamp", "datetime", "uuid")
# Declared type substrings treated as free or categorical text.
TEXT_SAMPLE_TYPES = ("char", "text", "string", "clob", "enum")


@dataclass(frozen=True)
class ColumnProfile:
    sample_values: list[Any]  # the top values, unfiltered: what the prompts see
    is_unique: bool
    format: str | None


@dataclass(frozen=True)
class TableProfile:
    """A table's profile: per column what the prompts read, and what the catalog keeps."""

    columns: dict[str, ColumnProfile] = field(default_factory=dict[str, ColumnProfile])
    # What upstream persists: sample values without dates, uuids or long strings
    stored_samples: dict[str, list[Any]] = field(default_factory=dict[str, list[Any]])

    @property
    def uniqueness(self) -> dict[str, bool]:
        return {name: c.is_unique for name, c in self.columns.items()}

    @property
    def date_formats(self) -> dict[str, str]:
        return {name: c.format for name, c in self.columns.items() if c.format}

    def as_prompt_input(self) -> dict[str, dict[str, Any]]:
        """Upstream's `columns_profiling_samples` shape, which the stage prompts read."""
        return {
            name: {"sample_values": c.sample_values, "is_unique": c.is_unique, "format": c.format}
            for name, c in self.columns.items()
        }


def qualified(schema: str | None, table: str) -> str:
    """The relation's name in the executor's DuckDB, whose default catalog is the source."""
    return f"{quote_ident(schema)}.{quote_ident(table)}" if schema else quote_ident(table)


def profile_table(
    executor: SourceExecutor,
    schema: str | None,
    table: str,
    columns: Sequence[dict[str, Any]],
    *,
    probe_distinct_values: bool = True,
) -> TableProfile:
    """Profile a table from a sample of up to 1,000 rows. `columns` are the catalog's, for their
    declared types. A sample that fails gives an empty profile: compile goes on without it."""
    name = qualified(schema, table)
    try:
        sample = executor.probe(
            f"SELECT * FROM {name} LIMIT {MAX_PROFILE_ROWS}",
            max_rows=MAX_PROFILE_ROWS,
            timeout_s=PROFILE_TIMEOUT_S,
        )
    except SourceError:
        logger.warning("[%s] column profiling query failed; skipping", table, exc_info=True)
        return TableProfile()

    declared = {c.get("name"): c.get("data_type") for c in columns if c.get("name")}
    profiles: dict[str, ColumnProfile] = {}
    stored: dict[str, list[Any]] = {}
    for index, column in enumerate(sample.columns):
        raw = [row[index] for row in sample.rows if row[index] is not None]
        values = [json_ready(v) for v in raw]
        keys = [sample_key(v) for v in values]
        is_unique = bool(keys) and len(set(keys)) == len(keys)
        first_by_key: dict[str, Any] = {}
        for key, value in zip(keys, values, strict=True):
            first_by_key.setdefault(key, value)
        top = [first_by_key[key] for key, _ in Counter(keys).most_common(TOP_N)]

        declared_type = declared.get(column)
        col_values = top
        if (
            probe_distinct_values
            and not is_unique
            and len(top) < TOP_N
            and _is_text_type(declared_type)
        ):
            distinct = _distinct_if_low_cardinality(executor, name, column)
            if distinct is not None:
                seen = {sample_key(v) for v in col_values}
                col_values = list(col_values)
                for value in distinct:
                    if (key := sample_key(value)) not in seen:
                        seen.add(key)
                        col_values.append(value)

        # SQLite and other loosely typed sources declare dates as text, so infer from the
        # values too; it only yields a format when every sampled value shares one notation.
        notation = (
            infer_date_format(raw)
            if is_date_type(declared_type) or _is_text_type(declared_type)
            else None
        )
        profiles[column] = ColumnProfile(col_values, is_unique, notation)
        if _is_excluded_type(declared_type):
            continue
        if kept := [v for v in col_values if _keep_persisted(v)]:
            stored[column] = kept
    return TableProfile(profiles, stored)


def _distinct_if_low_cardinality(
    executor: SourceExecutor, name: str, column: str
) -> list[Any] | None:
    """The column's full set of distinct values when it has at most `LOW_CARDINALITY_MAX`, else
    None, as it is on any error: the caller keeps the sample's most common values."""
    col = quote_ident(column)
    try:
        rows = executor.probe(
            f"SELECT DISTINCT {col} FROM {name} WHERE {col} IS NOT NULL "
            f"LIMIT {LOW_CARDINALITY_MAX + 1}",
            max_rows=LOW_CARDINALITY_MAX + 1,
            timeout_s=PROFILE_TIMEOUT_S,
        )
    except SourceError as e:
        logger.warning("distinct-value probe on %s.%s failed (%s)", name, column, e)
        return None
    values = [json_ready(row[0]) for row in rows.rows]
    if not values or len(values) > LOW_CARDINALITY_MAX:
        return None
    return values


def json_ready(value: Any) -> Any:
    """A source value as a JSON-serializable Python native (upstream's `_json_ready_sample`)."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, dict):
        items = cast(dict[Any, Any], value).items()
        return {str(k): json_ready(v) for k, v in items}
    if isinstance(value, list | tuple | set | frozenset):
        return [json_ready(v) for v in cast(Sequence[Any], value)]
    return str(value)


def sample_key(value: Any) -> str:
    """A grouping key for a sample value, unhashable ones included, that keeps `1`, `"1"` and
    `True` apart."""
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _keep_persisted(value: Any) -> bool:
    """Numbers and booleans always; text up to `MAX_SAMPLE_VALUE_LEN` characters."""
    if isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        return len(value) <= MAX_SAMPLE_VALUE_LEN
    return len(sample_key(value)) <= MAX_SAMPLE_VALUE_LEN


def _is_excluded_type(data_type: str | None) -> bool:
    return _has_token(data_type, EXCLUDED_SAMPLE_TYPES)


def _is_text_type(data_type: str | None) -> bool:
    return _has_token(data_type, TEXT_SAMPLE_TYPES)


def _has_token(data_type: str | None, tokens: tuple[str, ...]) -> bool:
    lowered = (data_type or "").lower()
    return any(token in lowered for token in tokens)
