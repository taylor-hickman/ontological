"""Query-history fragments and the SELECTs that wrap them (BUILD_PLAN.md M3.5): upstream's
extraction and wrapping rules, ported."""

from __future__ import annotations

import pytest

from ontological.catalog.parse_pool import ParsePool
from ontological.compile.fragments import (
    NoContext,
    build_valid_sql,
    extract_expressions,
    qualify_table_names,
)

HISTORY = (
    "SELECT c.Country, SUM(i.Total) FROM Invoice i JOIN Customer c ON i.CustomerId = c.CustomerId "
    "WHERE i.BillingCountry = 'USA' AND i.Total > 10 GROUP BY c.Country HAVING COUNT(*) > 2"
)


def test_filters_aggregates_and_having_are_extracted_and_key_equalities_are_not() -> None:
    got = extract_expressions(HISTORY)

    assert got == [
        "i.BillingCountry = 'USA'",
        "i.Total > 10",
        "SUM(i.Total)",
        "COUNT(*)",
        "COUNT(*) > 2",
    ]
    assert "i.CustomerId = c.CustomerId" not in got  # a bare key equality


def test_a_join_condition_with_a_literal_and_set_operations_are_extracted() -> None:
    joined = extract_expressions(
        "SELECT * FROM Track t JOIN Genre g ON t.GenreId = g.GenreId AND g.Name = 'Rock'"
    )
    assert "t.GenreId = g.GenreId AND g.Name = 'Rock'" in joined
    assert "g.Name = 'Rock'" in joined

    union = extract_expressions("SELECT ArtistId FROM Artist UNION SELECT ArtistId FROM Album")
    assert union == [
        "SELECT ArtistId FROM Artist UNION SELECT ArtistId FROM Album",
        "SELECT ArtistId FROM Artist",
        "SELECT ArtistId FROM Album",
    ]


def test_sql_that_does_not_parse_has_no_fragments() -> None:
    assert extract_expressions("") == []


def test_a_filter_becomes_a_where_with_only_the_joins_it_needs() -> None:
    sql = build_valid_sql("i.BillingCountry = 'USA'", [HISTORY], {"invoice": "main"})
    assert sql == "SELECT * FROM main.Invoice AS i WHERE i.BillingCountry = 'USA'"

    across = build_valid_sql(
        "c.Country = 'USA'", [HISTORY], {"invoice": "main", "customer": "main"}
    )
    assert across == (
        "SELECT * FROM main.Invoice AS i JOIN main.Customer AS c ON i.CustomerId = c.CustomerId "
        "WHERE c.Country = 'USA'"
    )


def test_an_aggregate_becomes_the_select_list_and_a_full_select_stays_as_it_is() -> None:
    assert build_valid_sql("SUM(i.Total)", [HISTORY]) == "SELECT SUM(i.Total) FROM Invoice AS i"
    assert build_valid_sql("SELECT 1 FROM Artist", [], {"artist": "main"}) == (
        "SELECT 1 FROM main.Artist"
    )


def test_a_fragment_with_no_statement_to_take_a_from_from_is_refused() -> None:
    with pytest.raises(NoContext):
        build_valid_sql("x.Total > 10", [])


def test_qualifying_leaves_qualified_and_unknown_tables_alone() -> None:
    sql = "SELECT * FROM other.Invoice AS i JOIN Nope AS n ON i.x = n.x"
    assert qualify_table_names(sql, {"invoice": "main"}) == sql


def test_the_parse_pool_extracts_in_order() -> None:
    with ParsePool(workers=1) as pool:
        got = pool.fragments([HISTORY, "SELECT * FROM Artist WHERE Name LIKE 'A%'"])
    assert got == [extract_expressions(HISTORY), ["Name LIKE 'A%'"]]
