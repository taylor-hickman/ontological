"""The month-counter fix (BUILD_PLAN.md M2.4, ARCHITECTURE.md §1.3): monthly query counts are
written and read under one key, and ordered by (year, month).

Upstream wrote `count_{month}_{year}` ("count_8_2026") and read `count_monthly_YYYY_MM`, so
no reader ever found a count. Its sort read the writer's `month_year` as `(year, month)` as well.
"""

# upstream's Query model is untyped.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from auto_ontology.catalog.model.query import Query
from ontological.catalog.query_stats import (
    Tally,
    get_sql_counters,
    month_key,
    month_of,
    parse_month_key,
    recent_usage,
)


def test_a_count_written_by_the_query_model_is_read_back_by_month() -> None:
    # The bug's case: upstream's Query wrote "count_8_2026" here, and get_sql_counters' readers
    # matched nothing.
    q = Query(schemas={}, id="q1", sql_text="SELECT 1", ltimestamp=datetime(2026, 8, 15), count=3)

    assert "count_8_2026" not in q.sql_node.get_properties()
    assert get_sql_counters(q.sql_node) == (3, {date(2026, 8, 1): 3})


def test_the_key_round_trips_and_is_zero_padded() -> None:
    assert month_key(datetime(2026, 1, 31, 23, 59)) == "count_monthly_2026_01"
    assert parse_month_key(month_key(date(2025, 12, 3))) == date(2025, 12, 1)


def test_upstream_s_key_and_other_properties_are_not_months() -> None:
    for key in ("count_8_2026", "count_12_2025", "total_counter", "count_monthly_2026_13"):
        assert parse_month_key(key) is None


def test_recent_usage_orders_months_by_year_then_month() -> None:
    # Upstream's sort key read count_12_2025 as (12, 2025), above any month of 2026.
    counts = {
        date(2025, 12, 1): 100,
        date(2026, 1, 1): 1,
        date(2026, 2, 1): 2,
        date(2026, 3, 1): 4,
    }
    assert recent_usage(counts) == 7
    assert recent_usage(counts, months=4) == 107


def test_a_time_with_a_zone_counts_in_its_utc_month() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert month_of(datetime(2026, 2, 1, 1, 0, tzinfo=plus_two)) == date(2026, 1, 1)
    assert month_of(datetime(2026, 2, 1, 1, 0)) == date(2026, 2, 1)  # naive: as given


def test_a_tally_keeps_the_total_the_months_and_the_latest_time() -> None:
    a, b = Tally(), Tally()
    a.add(2, datetime(2025, 12, 31, tzinfo=UTC))
    a.add(1, datetime(2026, 1, 2, tzinfo=UTC))
    b.add(5, datetime(2026, 1, 1, tzinfo=UTC))

    a.merge(b)

    assert a.total == 8
    assert a.months == {date(2025, 12, 1): 2, date(2026, 1, 1): 6}
    assert a.last == datetime(2026, 1, 2, tzinfo=UTC)
