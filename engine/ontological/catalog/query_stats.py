# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/catalog/query_stats.py, with types
# and the month-counter fix (ARCHITECTURE.md §1.3): one month key, written and read through the
# same two functions, ordered by (year, month).

"""Statistics about stored SQL statements, independent of where they are stored.

**The month-counter fix.** Upstream counts each sighting of a statement per month, and wrote the
count under `count_{month}_{year}` ("count_8_2026") while every reader matched
`count_monthly_YYYY_MM`. No name a reader accepts was ever written, so the counters held nothing
anything could use. Its sort key read the writer's `month_year` as `(year, month)` too, so even
with matching names December 2025 would have outranked January 2026.

Here the month of a sighting is `month_of`, the property-bag key is `month_key`, and the readers go
through `parse_month_key`. `get_sql_counters` returns the months as dates, which sort as (year,
month). The engine stores them in `app.sql_query_month`, one row per statement and month
(`query_history.py`).
"""

# pandas has no stubs here.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
# pyright: reportUnknownLambdaType=false

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

import pandas as pd

_MONTH_KEY = re.compile(r"^count_monthly_(\d{4})_(\d{2})$")


def month_of(ts: datetime | date) -> date:
    """The first day of the month `ts` falls in, in UTC when `ts` carries a time zone."""
    if isinstance(ts, datetime) and ts.tzinfo is not None:
        ts = ts.astimezone(UTC)
    return date(ts.year, ts.month, 1)


def month_key(ts: datetime | date) -> str:
    """The property name a statement's count for `ts`'s month is kept under:
    `count_monthly_YYYY_MM`."""
    month = month_of(ts)
    return f"count_monthly_{month.year:04d}_{month.month:02d}"


def parse_month_key(key: str) -> date | None:
    """The month `month_key` wrote `key` for, or None when `key` isn't a month counter."""
    m = _MONTH_KEY.match(key)
    if m is None:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


class _SqlNode(Protocol):
    def get_properties(self) -> dict[str, Any]: ...


def get_sql_counters(sql_node: _SqlNode) -> tuple[int, dict[date, int]]:
    """`(total_counter, {month: count})` from a statement's properties, each month the first day
    of it. Properties that aren't month counters are left out."""
    properties = sql_node.get_properties()
    months: dict[date, int] = {}
    for key, value in properties.items():
        month = parse_month_key(key)
        if month is not None:
            months[month] = months.get(month, 0) + int(value)
    return int(properties["total_counter"]), months


@dataclass
class Tally:
    """A statement's sightings: how many in all, how many per month, and the latest."""

    total: int = 0
    months: dict[date, int] = field(default_factory=dict[date, int])
    last: datetime | None = None

    def add(self, count: int, when: datetime) -> None:
        self.total += count
        month = month_of(when)
        self.months[month] = self.months.get(month, 0) + count
        if self.last is None or when > self.last:
            self.last = when

    def merge(self, other: Tally) -> None:
        """Fold a duplicate's sightings in. Upstream added the totals and months of two duplicates
        and kept the first one's time; this keeps the latest."""
        self.total += other.total
        for month, count in other.months.items():
            self.months[month] = self.months.get(month, 0) + count
        if other.last is not None and (self.last is None or other.last > self.last):
            self.last = other.last


def recent_usage(month_counts: Mapping[date, int], months: int = 3) -> int:
    """The sum of the `months` most recent monthly counts: upstream's intended usage score."""
    latest = sorted(month_counts, reverse=True)[:months]
    return sum(month_counts[m] for m in latest)


def get_candidate_sql_ids(
    tbl_ids: Iterable[str],
    col_ids: Iterable[str],
    nodes_count: int,
    sqls_tbls_df: pd.DataFrame,
    join_count: int = 0,
    union_count: int = 0,
) -> pd.DataFrame:
    """Pre-filter stored SQL by structural shape before comparing it properly.

    Same tables, same leaf columns, same AST size, same number of joins and unions. Only what
    survives all five gates is worth the expensive sqlglot structural comparison.
    """
    if sqls_tbls_df.empty:
        return sqls_tbls_df.iloc[0:0]

    tbl_set = set(tbl_ids)
    col_set = set(col_ids)
    mask = (
        (sqls_tbls_df["nodes_count"] == nodes_count)
        & (sqls_tbls_df["join_count"] == join_count)
        & (sqls_tbls_df["union_count"] == union_count)
        & sqls_tbls_df["tbls"].apply(lambda t: set(t) == tbl_set)
        & sqls_tbls_df["cols"].apply(lambda c: set(c) == col_set)
    )
    return sqls_tbls_df.loc[mask]
