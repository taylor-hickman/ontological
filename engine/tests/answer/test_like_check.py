"""Empty-result handling's LIKE step (`ontological.answer.like_check`): which LIKE and ILIKE
filters are dropped, and the probe of the query without them, on an in-memory DuckDB."""

from __future__ import annotations

import pytest
from fakes import DuckProber

from ontological.answer.like_check import SAMPLE_ROWS, drop_like_filters, probe_without_like
from ontological.answer.probe import ProbeExecutor


def dropped(sql: str) -> list[str]:
    found = drop_like_filters(sql, "duckdb")
    return [] if found is None else found[1]


@pytest.mark.parametrize(
    ("sql", "filters"),
    [
        ("SELECT a FROM t WHERE a LIKE 'x%'", ["a LIKE 'x%'"]),
        ("SELECT a FROM t WHERE a ILIKE '%x%' AND b = 1", ["a ILIKE '%x%'"]),
        ("SELECT a FROM t WHERE (a LIKE 'x' OR b ILIKE 'y')", ["a LIKE 'x'", "b ILIKE 'y'"]),
        ("SELECT a FROM t WHERE a NOT LIKE 'x%'", ["a NOT LIKE 'x%'"]),
        ("SELECT a FROM t WHERE a LIKE 'x!%' ESCAPE '!'", ["a LIKE 'x!%' ESCAPE '!'"]),
        ("SELECT a, COUNT(*) FROM t GROUP BY a HAVING a LIKE 'x'", ["a LIKE 'x'"]),
        ("SELECT t.a FROM t JOIN u ON u.id = t.id AND u.b LIKE 'x'", ["u.b LIKE 'x'"]),
        (
            "WITH r AS (SELECT a FROM t WHERE a ILIKE '%rock%') SELECT a FROM r",
            ["a ILIKE '%rock%'"],
        ),
        ("SELECT a FROM t WHERE id IN (SELECT id FROM u WHERE b LIKE 'x')", ["b LIKE 'x'"]),
    ],
)
def test_a_like_filter_where_true_can_only_loosen_is_dropped(sql: str, filters: list[str]) -> None:
    assert dropped(sql) == filters


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t WHERE a = 'x'",  # no LIKE
        "SELECT a FROM t WHERE NOT (a LIKE 'x' OR b = 1)",  # TRUE here keeps rows out
        "SELECT a FROM t WHERE CASE WHEN a LIKE 'x' THEN 0 ELSE 1 END = 1",
        "SELECT a LIKE 'x' AS starts FROM t",  # not a filter
        "SELECT FROM WHERE",  # doesn't parse
    ],
)
def test_a_like_filter_where_true_could_tighten_is_kept(sql: str) -> None:
    assert drop_like_filters(sql, "duckdb") is None


def test_the_like_filters_are_replaced_by_true_and_the_rest_stays() -> None:
    found = drop_like_filters("SELECT a FROM t WHERE a ILIKE '%x%' AND b = 1", "duckdb")
    assert found is not None
    assert found[0] == "SELECT a FROM t WHERE TRUE AND b = 1"


def genres(*names: str) -> DuckProber:
    values = ", ".join(f"({i}, '{n}')" for i, n in enumerate(names))
    return DuckProber(
        "CREATE TABLE genre (GenreId INTEGER, Name VARCHAR)",
        f"INSERT INTO genre VALUES {values}",
    )


def test_without_its_like_filter_the_empty_query_returns_rows() -> None:
    source = genres("Rock", "Jazz", "Metal")
    probes = ProbeExecutor(source)
    found = probe_without_like(
        probes, "duckdb", "SELECT Name FROM genre WHERE Name LIKE 'rock%' AND GenreId < 2"
    )
    assert found is not None
    assert found.dropped == ["Name LIKE 'rock%'"]
    assert found.columns == ["Name"] and found.rows == [["Rock"], ["Jazz"]]
    assert not found.truncated
    assert source.seen == ["SELECT Name FROM genre WHERE TRUE AND GenreId < 2"]
    assert probes.calls == 1


def test_a_like_probe_keeps_the_first_rows_and_says_there_were_more() -> None:
    source = genres(*(f"Genre {i}" for i in range(SAMPLE_ROWS + 2)))
    found = probe_without_like(
        ProbeExecutor(source), "duckdb", "SELECT Name FROM genre WHERE Name LIKE 'x'"
    )
    assert found is not None and len(found.rows) == SAMPLE_ROWS and found.truncated


def test_a_query_still_empty_without_its_like_filter_finds_nothing() -> None:
    sql = "SELECT Name FROM genre WHERE Name LIKE 'rock%' AND GenreId > 10"
    assert probe_without_like(ProbeExecutor(genres("Rock")), "duckdb", sql) is None


def test_a_like_probe_past_the_cap_or_failing_finds_nothing() -> None:
    sql = "SELECT Name FROM genre WHERE Name LIKE 'rock%'"
    spent = ProbeExecutor(genres("Rock"))
    spent.calls = spent.max_calls
    assert probe_without_like(spent, "duckdb", sql) is None
    assert probe_without_like(ProbeExecutor(None), "duckdb", sql) is None
    assert probe_without_like(ProbeExecutor(genres("Rock")), "duckdb", sql + " AND x = 1") is None


def test_sql_without_a_like_filter_spends_no_probe() -> None:
    probes = ProbeExecutor(genres("Rock"))
    assert probe_without_like(probes, "duckdb", "SELECT Name FROM genre WHERE Name = 'x'") is None
    assert probes.calls == 0
