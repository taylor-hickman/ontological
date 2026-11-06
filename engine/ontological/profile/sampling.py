# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: the profiling half of auto_ontology/semantic/visit_enter.py
# (`calculate_columns_profiling` and its helpers), moved to run inside the source executor's child
# (ARCHITECTURE.md §11). The edits: it returns the sample values, uniqueness and date formats it
# used to write to the store, and the engine writes them (`ontological/profile/run.py`); it
# queries through `Connector`, which the child builds on its DuckDB session, in place of an
# upstream connector; a failed row sample raises instead of returning `{}`, so the engine sees the
# error, and a cancelled DISTINCT probe re-raises rather than falling back; and the sampling
# circuit breaker moved to the engine with the timeouts it counts. `_MAX_EMBEDDED_JSON_SAMPLE_LEN`
# is copied from upstream's semantic/embed.py.
# ruff: noqa: SIM105  (upstream's code)
# The frames are pandas', which pyright can't see into.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false
# pyright: reportMissingTypeStubs=false, reportUnnecessaryComparison=false

"""Profile a table's columns from a capped sample of its rows, inside the source executor.

Upstream's `calculate_columns_profiling`, run in the executor's child so the sampled rows never
cross the pipe: only each column's top values, uniqueness and inferred format do.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Any, Protocol, TypedDict

import duckdb
import pandas as pd

from ontological.profile.date_format import infer_date_format, is_date_type

logger = logging.getLogger(__name__)

# Row cap for the per-column profiling sample.
_PROFILING_SAMPLE_LIMIT = 1000
# Most-common values kept per column.
_PROFILING_TOP_N = 5
# String sample values longer than this are not persisted.
_MAX_SAMPLE_VALUE_LEN = 30
# Declared data-type substrings whose sample values are not persisted.
_EXCLUDED_SAMPLE_TYPES = ("date", "time", "timestamp", "datetime", "uuid")
# A text column with at most this many distinct values is treated as
# categorical: we capture its full distinct value set (via a DISTINCT probe)
# instead of only the most-common values from the first-N-row sample. This
# ensures rare-but-meaningful enum values (e.g. 'Banned', 'Restricted') land in
# the embedded description even when the dominant value fills the row prefix.
_LOW_CARDINALITY_MAX = 25
# Declared data-type substrings treated as free/categorical text.
_TEXT_SAMPLE_TYPES = ("char", "text", "string", "clob", "enum")
# JSONB nested-key names that mark a sibling value as unit-qualified (e.g.
# {"value": 45000, "unit": "annual"}). When a container has one of these,
# every other key in that container is annotated with the unit key's path so
# the SQL generator sees, right next to the value, that it must not compare
# two such values without checking the unit matches.
_UNIT_MARKER_KEYS = ("unit", "units", "uom", "currency", "measure")
# JSONB nested-key name substrings that plausibly hold a date/timestamp
# string. Gates which nested keys are worth pulling example values for below
# — cheap and imprecise by design (a name-based heuristic, not a value-based
# one), since the alternative (capturing examples for every nested key) would
# bloat every JSONB column's description with mostly-irrelevant values.
_DATE_KEY_HINTS = ("date", "_dt", "day", "time", "moment", "schedule", "_on", "_at")
# Upstream's semantic/embed.py: the longest JSON-key sample entry the
# ColumnAttribute embedding text keeps.
_MAX_EMBEDDED_JSON_SAMPLE_LEN = 60

# Shape patterns for free-text values that hold a date/timestamp as a string
# (a JSONB nested leaf, or a "text"-typed column that was never cast to a
# real date type — both are common in these benchmark schemas). Matched
# against the *shape* only (digit widths / separators), never asserting a
# day/month reading for ambiguous slash-separated forms — that can't be
# recovered from the shape alone and a wrong guess would be worse than no
# guess. Order doesn't matter: a value is tested against every pattern and
# can only ever match one (they're mutually exclusive shapes).
_DATE_SHAPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("YYYY-MM-DD", re.compile(r"^\d{4}-\d{2}-\d{2}")),
    ("YYYY/MM/DD", re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")),
    ("DD/MM/YYYY or MM/DD/YYYY (ambiguous)", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("DD/MM/YY or MM/DD/YY (ambiguous)", re.compile(r"^\d{2}/\d{2}/\d{2}$")),
    ("Mon DD, YYYY", re.compile(r"^[A-Za-z]{3,9} \d{1,2},? \d{4}$")),
)


class Connector(Protocol):
    """What profiling needs from a source: upstream's connector, cut to these three."""

    def qualify(self, schema_name: str | None, table_name: str) -> str:
        """The table's name as a query names it."""
        ...

    def quote(self, name: str) -> str:
        """A column name, quoted for the source's SQL."""
        ...

    def execute(self, sql: str) -> pd.DataFrame:
        """One query's rows, with the values the driver returned (object columns)."""
        ...


class ColumnProfile(TypedDict):
    sample_values: list[Any]  # the top values, unfiltered: dates, uuids and long strings too
    is_unique: bool
    format: str | None


class TableProfile(TypedDict):
    """Upstream's return value under `columns`, and what it wrote to the store under the rest."""

    columns: dict[str, ColumnProfile]
    sample_values: dict[str, list[Any]]  # `store_column_sample_values`
    uniqueness: dict[str, bool]  # `store_column_uniqueness`
    date_formats: dict[str, str]  # `store_column_date_formats`


def empty_profile() -> TableProfile:
    return {"columns": {}, "sample_values": {}, "uniqueness": {}, "date_formats": {}}


def _is_excluded_sample_type(data_type: str | None) -> bool:
    """Whether a column's declared type disqualifies it from sample storage."""
    if not data_type:
        return False
    lowered = data_type.lower()
    return any(token in lowered for token in _EXCLUDED_SAMPLE_TYPES)


def _is_text_sample_type(data_type: str | None) -> bool:
    """Whether a column's declared type is free/categorical text."""
    if not data_type:
        return False
    lowered = data_type.lower()
    return any(token in lowered for token in _TEXT_SAMPLE_TYPES)


def _date_shapes_seen(values: Sequence[Any]) -> list[str]:
    """Distinct recognized date/timestamp shapes among *values*, in first-seen order.

    Non-string values are skipped rather than matched. The caller gates on the
    *declared* type, and that gate cannot tell a scalar from a collection of
    one: ClickHouse's ``Array(String)`` contains the ``string`` token, so an
    array column reaches here with ``list`` values, which ``re.match`` rejects
    with a TypeError. That aborted the whole table's profiling — caught one
    frame up and logged as "column profiling failed", so the samples,
    uniqueness and date formats for every column in the table were silently
    dropped. A non-string cannot carry a *textual* date shape anyway, which is
    the only thing this looks for.

    A value that matches none of :data:`_DATE_SHAPE_PATTERNS` is silently
    ignored rather than counted as "no date shape" for the whole column —
    this only exists to catch a column mixing more than one *recognized*
    shape (e.g. some rows ``YYYY-MM-DD``, others ``YYYY/MM/DD``), which is a
    real, observed bug: a fixed-format parser silently mis-parses whichever
    shape it wasn't written for. It intentionally does not try to classify
    every possible date-like string — an odd one-off format (e.g. a literal
    "12th Jun." style column) is left to speak for itself via the raw sample
    values already shown, rather than force a wrong or overly-broad match.
    """
    shapes: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for label, pattern in _DATE_SHAPE_PATTERNS:
            if pattern.match(value):
                if label not in shapes:
                    shapes.append(label)
                break
    return shapes


def _json_ready_sample(value: Any) -> Any:
    """Convert a live warehouse value to a JSON-serializable Python native.

    Drivers hand back types that no downstream consumer can serialize as-is:
    numpy scalars, ``Decimal``, ``datetime``, and the lists/dicts Postgres
    returns for array and JSON columns. Stringifying the whole sample up front
    is what this replaces — a number that survives as ``int`` stays a number
    all the way into storage, and only genuinely non-scalar values are
    rendered as text.

    numpy scalars are unwrapped by duck-typing ``.item()`` rather than by
    importing numpy, which is only present here transitively through pandas.
    """
    unwrap = getattr(value, "item", None)
    if callable(unwrap) and not isinstance(value, (str, bytes, bytearray)):
        try:
            value = unwrap()
        except Exception:
            pass

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _json_ready_sample(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_ready_sample(v) for v in value]
    return str(value)


def _sample_key(value: Any) -> str:
    """Stable grouping key for a sample value, including unhashable ones.

    Uniqueness and most-common counts both need to group equal values, but
    array and JSON columns come back as lists and dicts, which cannot go in a
    set. Keying on the JSON rendering keeps those columns countable while
    holding ``1``, ``"1"`` and ``True`` apart, which a plain ``str()`` key
    would collapse.
    """
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _keep_persisted_sample(value: Any) -> bool:
    """Whether a profiled value is short enough to persist as a sample.

    The length cap exists to keep prose and blobs out of prompts, so it only
    means anything for text: numbers and booleans are always worth persisting
    however many digits they have. Non-scalars are judged by their rendered
    length, since that is what a reader ultimately sees.
    """
    if isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= _MAX_SAMPLE_VALUE_LEN
    return len(_sample_key(value)) <= _MAX_SAMPLE_VALUE_LEN


def _distinct_values_if_low_cardinality(
    connector: Connector,
    qualified: str,
    col_name: str,
    cap: int,
) -> list[Any] | None:
    """Return the full distinct value set for a low-cardinality column.

    Runs ``SELECT DISTINCT <col> ... LIMIT cap + 1``. Returns the distinct
    values (in their native types) when the column has at most *cap* distinct
    non-null values; returns ``None`` for high-cardinality columns (more than *cap*
    distinct values) or on any error, so the caller falls back to the
    most-common-values behaviour. The ``LIMIT`` keeps the probe cheap even on
    huge, high-cardinality columns (the scan stops after cap + 1 distinct rows).
    A cancelled probe is not an error to fall back from: it stops the table.
    """
    quoted = connector.quote(col_name)
    try:
        df = connector.execute(
            f"SELECT DISTINCT {quoted} FROM {qualified} WHERE {quoted} IS NOT NULL LIMIT {cap + 1}",
        )
    except duckdb.InterruptException:
        raise
    except Exception as exc:
        logger.warning(
            "Distinct-value probe on %s.%s failed (%s) — using the row "
            "sample's most-common values instead",
            qualified,
            col_name,
            type(exc).__name__,
        )
        return None
    if df is None or df.empty:
        return None
    values = [_json_ready_sample(v) for v in df.iloc[:, 0].tolist()]
    if len(values) > cap:
        return None
    return values


def calculate_columns_profiling(
    table: dict[str, Any],
    columns: list[dict[str, Any]],
    connector: Connector,
    *,
    probe_distinct_values: bool = True,
) -> TableProfile:
    """Profile a table's columns from a live sample of up to 1000 rows.

    Runs ``SELECT * ... LIMIT 1000`` and, for every column, computes an
    ``is_unique`` flag (all non-null values distinct) and the 5 most-common
    values. For non-unique text columns whose sample yields fewer than 5
    distinct values, runs a ``SELECT DISTINCT`` probe to capture rare enum
    values that the row prefix may have missed.

    ``probe_distinct_values`` gates only that second step. The bounded
    ``SELECT * ... LIMIT 1000`` always runs; the DISTINCT probe is the
    unbounded one (a full-column scan per column on warehouses where
    ``DISTINCT`` + ``LIMIT`` does not early-stop), so it is what the
    "Distinct Value Scanning" setting turns off. With it off, a column keeps
    the top-N values from the row sample rather than gaining nothing.

    Sample values keep the type the warehouse returned (see
    ``_json_ready_sample``): a numeric column profiles as ``[10, 20, 30]``,
    not ``["10", "20", "30"]``. The types survive storage, since a column's
    ``sample_values`` is persisted as JSON; only readers that need display
    text coerce through ``stringify_sample_values``.

    Returns, for the engine to persist to catalog columns: ``is_unique`` for
    every column, ``format`` for date/time-typed or text columns whose sampled
    values share one storage notation — resolved by validating every value
    against strptime candidates (see ``ontological.profile.date_format``), so
    it can settle genuinely ambiguous-looking shapes (``13/07/2011`` can only
    be day-first) rather than merely flag them — and ``sample_values`` for
    every column except those whose declared type is a date/time/uuid
    (individual string values longer than 30 chars are dropped). A text column
    whose real values mix more than one recognized date/timestamp shape (e.g.
    some rows ``YYYY-MM-DD``, others ``YYYY/MM/DD``) gets an explicit
    mixed-format warning appended, checked against the full sample rather than
    only the stored top-5 so a rare minority shape isn't missed. JSONB nested
    keys whose name hints at a date (see ``_DATE_KEY_HINTS``) get the same
    treatment plus a raw example value when no recognized shape matches at
    all.

    ``columns`` in the result is ``{column_name: {"sample_values": [top-5
    values], "is_unique": bool, "format": str | None}}`` for *all* columns
    (values unfiltered — includes dates, uuids and long strings).

    A row sample that fails raises, for the engine to log and count.
    """
    schema_name = table.get("schema_name")
    table_name = table["name"]
    # Qualification is the connector's rule, not ours: engines with a
    # catalog.schema.table namespace prepend their bound catalog here, and a
    # two-level name would resolve against the wrong catalog.
    qualified = connector.qualify(schema_name, table_name)

    df = connector.execute(f"SELECT * FROM {qualified} LIMIT {_PROFILING_SAMPLE_LIMIT}")

    if df is None or df.empty:
        return empty_profile()

    type_by_column = {col.get("name"): col.get("data_type") for col in columns if col.get("name")}

    profiling: dict[str, ColumnProfile] = {}
    sample_values: dict[str, list[Any]] = {}
    uniqueness: dict[str, bool] = {}
    date_formats: dict[str, str] = {}

    for column in df.columns:
        col_name = str(column)
        try:
            series = df[column].dropna()
            values = [_json_ready_sample(v) for v in series.tolist()]
            # Group through _sample_key rather than the values themselves:
            # Postgres array columns come back as Python lists and JSON/JSONB
            # as dict/list, neither of which can go in a set or a Counter.
            keys = [_sample_key(value) for value in values]

            is_unique = bool(keys and len(set(keys)) == len(keys))

            first_by_key: dict[str, Any] = {}
            for key, value in zip(keys, values, strict=True):
                first_by_key.setdefault(key, value)
            top5 = [first_by_key[key] for key, _ in Counter(keys).most_common(_PROFILING_TOP_N)]
        except Exception:
            logger.warning(
                "[%s] profiling failed for column %r — skipping column",
                table_name,
                col_name,
                exc_info=True,
            )
            continue

        declared_type = type_by_column.get(col_name)

        # For categorical text columns that did not yield a full top-N set from
        # the first-N-row sample, prefer the full distinct value set. Rare enum
        # values (e.g. 'Banned') otherwise never make it into the embedded
        # description when a dominant value fills the sampled row prefix.
        # Skip the DISTINCT probe when the sample already produced _PROFILING_TOP_N
        # values — that is enough for embedding and avoids a full-table scan
        # per column on warehouses where DISTINCT + LIMIT does not early-stop.
        col_values = top5
        if (
            probe_distinct_values
            and not is_unique
            and len(top5) < _PROFILING_TOP_N
            and _is_text_sample_type(declared_type)
        ):
            distinct_vals = _distinct_values_if_low_cardinality(
                connector, qualified, col_name, _LOW_CARDINALITY_MAX
            )
            if distinct_vals is not None:
                merged = list(top5)
                seen_keys = {_sample_key(value) for value in merged}
                for value in distinct_vals:
                    key = _sample_key(value)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        merged.append(value)
                col_values = merged

        # SQLite (and other loosely-typed sources) declare dates as TEXT, so the
        # declared type alone misses them: infer from the values as well, which
        # only yields a format when every sampled value shares one notation.
        # Unlike main's port, this does NOT suppress sample_values below just
        # because a format was found — samples stay available to FK inference
        # (semantic_fk.py's SQL-probe fallback reads persisted sample_values),
        # and date values are short enough that keeping them costs little.
        date_format = (
            infer_date_format(series)
            if is_date_type(declared_type) or _is_text_sample_type(declared_type)
            else None
        )
        if date_format:
            date_formats[col_name] = date_format

        uniqueness[col_name] = is_unique
        profiling[col_name] = {
            "sample_values": col_values,
            "is_unique": is_unique,
            "format": date_format,
        }

        if _is_excluded_sample_type(declared_type):
            continue

        # JSONB columns: the raw value is a Python dict and its string
        # representation always exceeds _MAX_SAMPLE_VALUE_LEN, so the normal
        # filter would silently discard everything.  Instead, extract the
        # unique key names from the sampled rows — flat keys at the top level
        # and one level of nesting — so the SQL generator sees the actual
        # field paths it needs to write correct ->/->> expressions.
        if "json" in (declared_type or "").lower():
            top_level_keys: list[str] = []
            containers: dict[str, list[str]] = {}
            # Example raw values per (container, nested_key) — only collected
            # for keys whose name hints at a date/timestamp (see
            # _DATE_KEY_HINTS); every other nested key stays name-only, same
            # as before. These are never a Column's real "sample_values" (the
            # column itself is JSONB, not text) — they exist solely to let a
            # date-shape check run on a nested leaf the way it already can on
            # a plain text column.
            nested_date_examples: dict[tuple[str, str], list[str]] = {}
            # Example raw values per top-level flat key (e.g. 'Tx_Adh' ->
            # ['High', 'Medium', 'Compliant']). Unlike nested_date_examples
            # above, this is collected for every flat key, not just
            # date-hinted ones — a bare key name like 'Tx_Adh' gives the SQL
            # generator no way to tell it apart from a same-purpose flat
            # column (e.g. 'med_adh') that already shows its real sample
            # values; showing the actual values here closes that gap.
            top_level_examples: dict[str, list[str]] = {}
            for raw_val in df[column].dropna():
                if not isinstance(raw_val, dict):
                    continue
                for k, v in raw_val.items():
                    if isinstance(v, dict):
                        nested = containers.setdefault(k, [])
                        for nested_k, nested_v in v.items():
                            if nested_k not in nested:
                                nested.append(nested_k)
                            is_date_hint = any(hint in nested_k.lower() for hint in _DATE_KEY_HINTS)
                            if nested_v is None or not is_date_hint:
                                continue
                            examples = nested_date_examples.setdefault((k, nested_k), [])
                            str_v = str(nested_v)
                            if (
                                len(examples) < 3
                                and str_v not in examples
                                and (len(str_v) <= _MAX_SAMPLE_VALUE_LEN)
                            ):
                                examples.append(str_v)
                    else:
                        if k not in top_level_keys:
                            top_level_keys.append(k)
                        if v is None:
                            continue
                        examples = top_level_examples.setdefault(k, [])
                        str_v = str(v)
                        if (
                            len(examples) < _PROFILING_TOP_N
                            and str_v not in examples
                            and (len(str_v) <= _MAX_SAMPLE_VALUE_LEN)
                        ):
                            examples.append(str_v)

            # embed.py's ColumnAttribute embedding text (_format_sample_values)
            # drops any individual sample_values entry over
            # _MAX_EMBEDDED_JSON_SAMPLE_LEN chars WHOLE, not truncated — so an
            # over-length annotation risks losing the entry, including the
            # bare key name, from retrieval entirely (this was already true
            # pre-existing for the WARNING/date-format annotations below,
            # which had no fallback at all). ``_safe_annotate`` keeps
            # appending as much annotation as fits, falling back a step at a
            # time rather than an all-or-nothing choice, so a key's own name
            # is never lost just because a longer example/format string
            # didn't fit next to it. The SQL-gen prompt (sql_from_semantic.py)
            # applies no such filter — it always sees the full, un-capped
            # entry — so this only trims what retrieval-time embedding sees.
            # Note this budgets the *formatted* entry (brackets/quotes
            # included) against _MAX_EMBEDDED_JSON_SAMPLE_LEN (the JSON-only,
            # 60-char budget), not the raw-value cutoff (_MAX_SAMPLE_VALUE_LEN,
            # 30 chars) used above when deciding whether a value was worth
            # collecting as an example candidate at all — those are two
            # different questions with two different budgets.
            def _safe_annotate(base: str, *annotations: str) -> str:
                entry = base
                for ann in annotations:
                    candidate = f"{entry} {ann}"
                    if len(candidate) <= _MAX_EMBEDDED_JSON_SAMPLE_LEN:
                        entry = candidate
                    else:
                        break
                return entry

            json_keys: list[str] = []
            for k, nested_keys in containers.items():
                unit_key = next(
                    (nk for nk in nested_keys if nk.lower() in _UNIT_MARKER_KEYS),
                    None,
                )
                for nested_k in nested_keys:
                    base = f"{k}.{nested_k}"
                    unit_ann = (
                        f"[unit: {k}.{unit_key}]" if unit_key and nested_k != unit_key else None
                    )
                    examples = nested_date_examples.get((k, nested_k))
                    date_ann = None
                    if examples:
                        shapes = _date_shapes_seen(examples)
                        if len(shapes) > 1:
                            # Try every observed shape first (this is the one
                            # place fewer items changes the *meaning*, not
                            # just the detail level — dropping a shape here
                            # could hide a real mixed-format bug), then fall
                            # back to just flagging that shapes are mixed
                            # without enumerating them, before giving up the
                            # annotation entirely.
                            full = f"[WARNING mixed date formats observed: {', '.join(shapes)}]"
                            short = "[WARNING mixed date formats observed]"
                            date_ann = (
                                full
                                if len(f"{base} {full}") <= _MAX_EMBEDDED_JSON_SAMPLE_LEN
                                else short
                            )
                        elif shapes:
                            date_ann = f"[date format: {shapes[0]}]"
                        else:
                            date_ann = f"[e.g. {examples[0]!r}]"
                    anns = [a for a in (unit_ann, date_ann) if a]
                    json_keys.append(_safe_annotate(base, *anns))
            for k in top_level_keys:
                examples = top_level_examples.get(k)
                if not examples:
                    json_keys.append(k)
                    continue
                # Greedily fit as many distinct example values as the length
                # budget allows (1 up to len(examples), all collected up to
                # _PROFILING_TOP_N) instead of a fixed count — a short key
                # with short values (e.g. 'Tx_Adh': 'High'/'Low'/'Compliant')
                # gets more signal than a fixed cap of 1 would give it, while
                # a long key/value pair still degrades gracefully to fewer
                # examples, then to the bare key, rather than being dropped.
                best = k
                for n in range(1, len(examples) + 1):
                    candidate = f"{k} [e.g. {', '.join(repr(e) for e in examples[:n])}]"
                    if len(candidate) <= _MAX_EMBEDDED_JSON_SAMPLE_LEN:
                        best = candidate
                    else:
                        break
                json_keys.append(best)

            if json_keys:
                # Capped well above the typical real key count (see
                # mental_health.treatmentoutcomes.txprogmet, which has 11) so
                # a wide-but-not-huge JSONB column doesn't lose keys to a
                # cap tuned for a narrower column shape seen elsewhere.
                sample_values[col_name] = json_keys[:20]
            continue

        filtered = [v for v in col_values if _keep_persisted_sample(v)]
        if _is_text_sample_type(declared_type):
            # Checked against the full up-to-1000-row sample, not just the
            # top-5/distinct values kept for display — a minority format can
            # be entirely absent from the top-5 while still breaking a
            # fixed-format parser on real rows (observed: a column mostly
            # 'YYYY-MM-DD' with a rare 'YYYY/MM/DD' minority).
            shapes = _date_shapes_seen(list(series))
            if len(shapes) > 1:
                filtered.append(f"[WARNING mixed date formats observed: {', '.join(shapes)}]")
        if filtered:
            sample_values[col_name] = filtered

    return {
        "columns": profiling,
        "sample_values": sample_values,
        "uniqueness": uniqueness,
        "date_formats": date_formats,
    }
