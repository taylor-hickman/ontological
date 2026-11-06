"""`validate_sql`'s gate, checks 1 to 3 (ARCHITECTURE.md §8.2, BUILD_PLAN.md M1.7), on a catalog
written by hand: one read-only statement, every table and column in the catalog, and no
degenerate SQL or raw many-side column under a GROUP BY. Checks 4 to 7 are M4.1's."""

from __future__ import annotations

import pytest
from ontological.answer.gate import KeyColumns, check_sql

from ontological.catalog.parsers.catalog_index import CatalogIndex, SchemaEntry, TableEntry

CATALOG = CatalogIndex(
    {
        "main": SchemaEntry(
            {
                "artist": TableEntry("t-artist", "Artist", {"artistid": "c1", "name": "c2"}),
                "album": TableEntry(
                    "t-album", "Album", {"albumid": "c3", "title": "c4", "artistid": "c5"}
                ),
                "track": TableEntry(
                    "t-track",
                    "Track",
                    {"trackid": "c6", "name": "c7", "albumid": "c8", "milliseconds": "c9"},
                ),
            }
        )
    }
)
KEYS = {"artist": ["artistid"], "album": ["albumid"], "track": ["trackid"]}


def keys(table: str) -> KeyColumns:
    return KeyColumns(pk=KEYS.get(table.lower(), []), unique=[])


def check(sql: str) -> list[str]:
    return check_sql(sql, CATALOG, key_columns=keys).issues


def test_a_query_over_catalog_tables_passes_and_names_what_it_used() -> None:
    gate = check_sql(
        "SELECT ar.Name, COUNT(al.AlbumId) AS albums FROM Artist ar "
        "JOIN Album al ON al.ArtistId = ar.ArtistId GROUP BY ar.Name",
        CATALOG,
        key_columns=keys,
    )

    assert gate.ok and gate.issues == []
    assert gate.tables == ["main.Album", "main.Artist"]
    assert gate.columns == ["Album.AlbumId", "Album.ArtistId", "Artist.ArtistId", "Artist.Name"]


@pytest.mark.parametrize(
    "sql",
    [
        "WITH n AS (SELECT Name FROM Artist) SELECT Name FROM n",
        "SELECT Title FROM Album UNION SELECT Name FROM Artist",
        "SELECT Title FROM main.Album",
        'SELECT "Title" FROM "Album"',
        "SELECT Title FROM Album WHERE EXISTS (SELECT 1 FROM Artist a WHERE a.ArtistId = 1)",
        "SELECT x FROM (SELECT Title AS x FROM Album) s",
        "SELECT Title FROM Album;",
    ],
)
def test_ordinary_read_queries_pass(sql: str) -> None:
    assert check(sql) == []


# --- check 1: one read-only statement ---------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM Album",
        "UPDATE Album SET Title = 'x'",
        "INSERT INTO Album VALUES (1, 'x', 1)",
        "CREATE TABLE t AS SELECT 1",
        "DROP TABLE Album",
        "ATTACH 'other.db' AS other",
        "COPY Album TO 'album.csv'",
        "PRAGMA table_info('Album')",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET threads = 1",
    ],
)
def test_anything_but_a_select_is_rejected(sql: str) -> None:
    [issue] = check(sql)
    assert "only a single SELECT" in issue


def test_two_statements_are_rejected() -> None:
    [issue] = check("SELECT Title FROM Album; SELECT Name FROM Artist")
    assert "exactly one statement" in issue


def test_sql_that_does_not_parse_is_rejected_with_the_parser_error() -> None:
    [issue] = check("SELECT Title FROM Album WHERE")
    assert issue.startswith("The SQL doesn't parse as DuckDB SQL")


def test_empty_sql_is_rejected() -> None:
    [issue] = check("  ;")
    assert "empty" in issue


# --- check 2: every table and column in the catalog -------------------------------------------


def test_an_unknown_table_is_named_with_the_nearest_catalog_tables() -> None:
    [issue] = check("SELECT Title FROM Albums")
    assert "Albums" in issue and "Album" in issue


def test_an_unknown_column_is_named() -> None:
    [issue] = check("SELECT Titel FROM Album")
    assert "titel" in issue.lower()


def test_an_unknown_qualified_column_is_named() -> None:
    [issue] = check("SELECT a.Nme FROM Artist a")
    assert "nme" in issue.lower()


def test_a_database_qualifier_is_rejected() -> None:
    [issue] = check("SELECT Title FROM chinook.main.Album")
    assert "chinook" in issue


def test_a_query_that_reads_no_catalog_table_is_rejected() -> None:
    [issue] = check("SELECT 1 + 1 AS two")
    assert "no table" in issue


# --- check 3: degenerate SQL and missing aggregation ------------------------------------------


@pytest.mark.parametrize(
    ("sql", "says"),
    [
        ("SELECT NULL FROM Album", "constant"),
        ("SELECT 1 AS one FROM Album", "constant"),
        ("SELECT Title FROM Album WHERE 1 = 0", "always-false"),
        ("SELECT Title FROM Album WHERE FALSE", "always-false"),
        ("SELECT Title FROM Album LIMIT 0", "LIMIT 0"),
    ],
)
def test_degenerate_sql_is_rejected(sql: str, says: str) -> None:
    [issue] = check(sql)
    assert says in issue and "placeholder" in issue


def test_a_raw_many_side_column_under_a_group_by_is_rejected() -> None:
    [issue] = check(
        "SELECT al.Title, t.Milliseconds FROM Album al JOIN Track t ON t.AlbumId = al.AlbumId "
        "GROUP BY al.AlbumId, al.Title"
    )
    assert "t.Milliseconds" in issue and "GROUP BY is unsafe" in issue


def test_an_aggregated_many_side_column_under_a_group_by_passes() -> None:
    assert (
        check(
            "SELECT al.Title, SUM(t.Milliseconds) AS ms FROM Album al "
            "JOIN Track t ON t.AlbumId = al.AlbumId GROUP BY al.AlbumId, al.Title"
        )
        == []
    )
