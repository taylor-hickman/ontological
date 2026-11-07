"""The pre-compile estimate's arithmetic (BUILD_PLAN.md M3.7, ARCHITECTURE.md §6.5): the calls
counted as compile makes them, from the plan's packs, and each call's time and cost from its
route's rate plus S2's measured spawn overhead."""

from __future__ import annotations

from collections import Counter

import pytest

from ontological.compile import estimate as e
from ontological.compile.estimate import Call, EstimatedTable

# Two small tables that share a pack, and a wide one that is a pack of its own
ALBUM = EstimatedTable("t1", "main", "Album", columns=3, non_fk=3, undescribed=3, candidates=2)
BAND = EstimatedTable(
    "t2", "main", "Band", columns=4, non_fk=3, undescribed=2, candidates=2, described=True
)
WIDE = EstimatedTable(
    "t3", "main", "Wide", columns=50, non_fk=45, undescribed=45, candidates=44, key_like=30
)


def tasks(calls: list[Call]) -> Counter[str]:
    return Counter(c.task for c in calls)


def test_split_asks_each_stage_once_a_pack_and_wide_tables_in_chunks() -> None:
    small = e.split_calls([ALBUM, BAND])
    # keys, one packed descriptions call (5 columns), terms, and synonyms for Band's description
    assert small == [
        Call("table_keys", 2, 7),
        Call("column_descriptions", 2, 5),
        Call("table_terms", 2, 6),
        Call("table_synonyms", 1),
    ]
    wide = e.split_calls([WIDE])
    # 45 undescribed columns in batches of 15, and 45 candidates in two chunks of at most 40
    assert tasks(wide) == {"table_keys": 1, "column_descriptions": 3, "table_terms": 2}
    assert [c.columns for c in wide if c.task == "column_descriptions"] == [15, 15, 15]


def test_merged_asks_a_call_a_table_and_leaves_wide_and_pure_fk_tables_to_split() -> None:
    bridge = EstimatedTable(
        "t4", "main", "Bridge", columns=2, non_fk=0, undescribed=0, candidates=0
    )
    calls = e.merged_calls([ALBUM, BAND, WIDE, bridge])
    assert tasks(calls) == {
        "table_semantics": 2,  # Album and Band
        "table_keys": 1,  # Wide's split stages; Bridge, two declared FKs, has nothing to ask
        "column_descriptions": 3,
        "table_terms": 2,
    }
    assert Call("table_semantics", 1, 3) in calls


def test_the_estimate_counts_both_options_with_the_same_post_stages() -> None:
    got = e.estimate("music", [WIDE, BAND, ALBUM], bridges=1)
    # 30 key-like columns: two semantic-FK picks of 25 and 5, then the bridge's SQL
    assert got.tables == 3
    assert (got.split.calls, got.merged.calls) == (4 + 6 + 3, 2 + 6 + 3)
    assert got.split.calls_by_model == {"haiku": 5, "opus": 1, "sonnet": 7}
    # One table call in ten may be answered invalidly: split asks it again, merged asks it again
    # and then the table's split calls
    assert (got.split.calls_high, got.merged.calls_high) == (13 + 1, 11 + 5)


def test_a_call_takes_its_turn_plus_s2_s_spawn_overhead() -> None:
    keys = Call("table_keys", tables=2, columns=7)
    assert e.call_seconds(keys) == pytest.approx(0.53 + 1.6 + 1.2 * 2)
    assert e.call_usd(keys) == pytest.approx(0.004 + 0.0055 * 2)
    described = Call("column_descriptions", tables=2, columns=5)
    assert e.call_seconds(described) == pytest.approx(0.53 + 1.7 + 0.12 * 5)
    assert e.call_usd(described) == pytest.approx(0.0003 + 0.00004 * 5)


def test_a_merged_call_is_the_four_stages_parts_on_sonnet() -> None:
    rate = e.RATES["table_semantics"]
    assert rate.model == "sonnet"
    assert rate.table_s == pytest.approx(1.2 + 1.1 + 0.3)
    assert rate.column_s == pytest.approx(0.12)
    # Haiku's parts at Sonnet's prices, twenty times Haiku's
    assert rate.table_usd == pytest.approx(0.0055 + 0.004 + 0.0001 * 20)
    assert rate.column_usd == pytest.approx(0.00004 * 20)


def test_each_figure_s_range_spans_the_point_estimate() -> None:
    got = e.estimate("music", [ALBUM, BAND]).split
    calls = e.split_calls([ALBUM, BAND])
    assert got.seconds == pytest.approx(sum(e.call_seconds(c) for c in calls), abs=0.05)
    assert got.seconds == pytest.approx(
        sum(e.call_seconds(c) - 0.53 for c in calls) + 0.53 * 4, abs=0.05
    )
    assert (got.seconds_low, got.seconds_high) == pytest.approx(
        (got.seconds * 0.7, got.seconds * 1.5), abs=0.1
    )
    assert (got.usd_low, got.usd_high) == pytest.approx((got.usd * 0.7, got.usd * 1.5), abs=1e-4)


def test_nothing_to_compile_costs_nothing() -> None:
    got = e.estimate("music", [])
    assert (got.split.calls, got.merged.calls, got.split.usd, got.merged.seconds) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    ("name", "key_like"),
    [("ArtistId", True), ("artist_id", True), ("ISBN", False), ("id", False), ("api_key", True)],
)
def test_a_column_named_like_a_key(name: str, key_like: bool) -> None:
    assert e.is_key_like(name) is key_like
