"""`validate_sql`'s gate (ARCHITECTURE.md §8.2, BUILD_PLAN.md M1.7 and M4.1), on catalogs written
by hand. Checks 1 to 3: one read-only statement, every table and column in the catalog, and no
degenerate SQL or raw many-side column under a GROUP BY. Checks 4 to 7, probed against an
in-memory DuckDB source: joins the semantic-FK graph bears out, literals the source holds, JSON
keys it holds, and no Postgres-only rules for DuckDB SQL. The checks themselves are tested in
`test_case_dirty_join_check.py`, `test_literal_check.py` and `test_jsonb_path_check.py`."""

from __future__ import annotations

import pytest
from fakes import DuckProber, FakeGraph, hop

from ontological.answer.gate import GateResult, KeyColumns, Probing, check_sql
from ontological.answer.probe import ProbeExecutor
from ontological.catalog.parsers.catalog_index import CatalogIndex, SchemaEntry, TableEntry
from ontological.sources.executor import Rows, SourceQueryError


def table(id: str, name: str, columns: list[str]) -> TableEntry:
    return TableEntry(
        id,
        name,
        {c.lower(): f"{id}.{c}" for c in columns},
        {c.lower(): c for c in columns},
    )


CATALOG = CatalogIndex(
    {
        "main": SchemaEntry(
            {
                "artist": table("t-artist", "Artist", ["ArtistId", "Name"]),
                "album": table("t-album", "Album", ["AlbumId", "Title", "ArtistId"]),
                "track": table("t-track", "Track", ["TrackId", "Name", "AlbumId", "Milliseconds"]),
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


# --- checks 4 to 7: probed against the source --------------------------------------------------

SOURCE = (
    "CREATE TABLE Artist (ArtistId INTEGER, Name VARCHAR)",
    "CREATE TABLE Album (AlbumId INTEGER, Title VARCHAR, ArtistId INTEGER, ArtistName VARCHAR)",
    "CREATE TABLE TrackInfo (TrackId INTEGER, Info JSON)",
    "INSERT INTO Artist VALUES (1, 'AC/DC'), (2, 'Accept')",
    "INSERT INTO Album VALUES (10, 'For Those About To Rock', 1, 'AC/DC'), "
    "(11, 'Balls to the Wall', 2, 'accept'), (12, 'Restless and Wild', 2, 'Accept')",
    """INSERT INTO TrackInfo VALUES (1, '{"bpm": 120, "mood": "loud"}')""",
)
PROBED = CatalogIndex(
    {
        "main": SchemaEntry(
            {
                "artist": table("t-artist", "Artist", ["ArtistId", "Name"]),
                "album": table("t-album", "Album", ["AlbumId", "Title", "ArtistId", "ArtistName"]),
                "trackinfo": TableEntry(
                    "t-info",
                    "TrackInfo",
                    {"trackid": "t-info.TrackId", "info": "t-info.Info"},
                    {"trackid": "TrackId", "info": "Info"},
                    {"trackid": "INTEGER", "info": "JSON"},
                ),
            }
        )
    }
)


def graph() -> FakeGraph:
    return FakeGraph(
        {("Album.ArtistId", "Artist.ArtistId"): [hop("Album.ArtistId", "Artist.ArtistId")]},
        fk_columns={"Album.ArtistId", "Artist.ArtistId"},
    )


def probed(sql: str, *, soft_as_warnings: bool = False) -> tuple[GateResult, DuckProber]:
    source = DuckProber(*SOURCE)
    gate = check_sql(
        sql,
        PROBED,
        key_columns=keys,
        probing=Probing(ProbeExecutor(source), graph()),
        soft_as_warnings=soft_as_warnings,
    )
    return gate, source


def test_a_query_the_source_bears_out_passes_every_check() -> None:
    gate, source = probed(
        "SELECT ar.Name, COUNT(*) AS albums FROM Album al "
        "JOIN Artist ar ON al.ArtistId = ar.ArtistId WHERE ar.Name = 'Accept' GROUP BY ar.Name"
    )

    assert gate.ok and gate.warnings == []
    assert gate.tables == ["main.Album", "main.Artist"]
    assert source.seen  # the literal was probed


def test_probes_wait_for_checks_1_to_3() -> None:
    gate, source = probed("SELECT Nmae FROM Artist WHERE Name = 'acdc'")

    assert not gate.ok and source.seen == []


def test_without_a_source_checks_4_to_6_dont_run() -> None:
    assert check("SELECT Name FROM Artist WHERE Name = 'ac/dc'") == []


def test_check_4_rejects_a_join_the_graph_and_the_values_dont_bear_out() -> None:
    gate, _ = probed("SELECT al.Title FROM Album al JOIN Artist ar ON ar.ArtistId = al.AlbumId")

    [issue] = gate.issues  # the graph has no path between these columns, and no value overlaps
    assert "Artist.ArtistId = Album.AlbumId" in issue and "very likely wrong" in issue


def test_check_4_case_dirty_join_is_an_issue_until_soft_checks_turn_to_warnings() -> None:
    sql = (
        "SELECT al.Title FROM Album al "
        "JOIN Artist ar ON al.ArtistId = ar.ArtistId AND al.ArtistName = ar.Name"
    )

    gate, _ = probed(sql)
    [issue] = gate.issues
    assert "LOWER(TRIM(" in issue and "1 row(s)" in issue

    gate, _ = probed(sql, soft_as_warnings=True)
    assert gate.ok and gate.issues == []
    [warning] = gate.warnings
    assert "LOWER(TRIM(" in warning


def test_check_5_rejects_a_near_miss_literal_until_soft_checks_turn_to_warnings() -> None:
    sql = (
        "SELECT Title FROM Album al JOIN Artist ar ON al.ArtistId = ar.ArtistId "
        "WHERE ar.Name = 'ac/dc'"
    )

    gate, _ = probed(sql)
    [issue] = gate.issues
    assert "'ac/dc'" in issue and "Closest real value(s): AC/DC." in issue

    gate, _ = probed(sql, soft_as_warnings=True)
    assert gate.ok and len(gate.warnings) == 1


def test_check_4s_wrong_join_stays_an_issue_when_soft_checks_are_warnings() -> None:
    gate, _ = probed(
        "SELECT al.Title FROM Album al JOIN Artist ar ON ar.ArtistId = al.AlbumId",
        soft_as_warnings=True,
    )

    assert not gate.ok and gate.warnings == []


def test_check_6_rejects_a_json_key_the_source_doesnt_hold() -> None:
    gate, source = probed("SELECT Info->>'tempo' AS tempo FROM TrackInfo")

    [issue] = gate.issues
    assert "TrackInfo.Info->>'tempo'" in issue and "bpm, mood" in issue
    assert any("json_keys" in probe for probe in source.seen)


def test_check_6_passes_a_json_key_the_source_holds() -> None:
    gate, _ = probed("SELECT Info->>'bpm' AS bpm FROM TrackInfo")

    assert gate.ok


def test_check_6_stays_an_issue_when_soft_checks_are_warnings() -> None:
    gate, _ = probed("SELECT Info->>'tempo' AS tempo FROM TrackInfo", soft_as_warnings=True)

    assert not gate.ok


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT DISTINCT ON (ArtistId) ArtistId, Title FROM Album ORDER BY ArtistId, Title",
        "SELECT artist.name FROM ARTIST",
        'SELECT "Name" FROM "Artist"',
    ],
)
def test_check_7_duckdb_sql_isnt_held_to_postgres_rules(sql: str) -> None:
    """No `DISTINCT ON` rejection and no mixed-case quoting: DuckDB runs both as written."""
    gate, source = probed(sql)

    assert gate.ok, gate.issues
    assert source.probe(sql, max_rows=5).rows


def test_a_probe_the_source_fails_doesnt_fail_the_gate() -> None:
    class Broken:
        def probe(self, sql: str, *, max_rows: int) -> Rows:
            raise SourceQueryError("the source is gone")

    gate = check_sql(
        "SELECT Name FROM Artist WHERE Name = 'ac/dc'",
        PROBED,
        key_columns=keys,
        probing=Probing(ProbeExecutor(Broken()), graph()),
    )

    assert gate.ok


def test_a_join_check_whose_probes_cant_run_doesnt_fail_the_gate() -> None:
    """An FK-shaped join with no graph path, on a source whose probes fail: check 4 is hard,
    so a probe that can't run must never become a rejection."""

    class Broken:
        def probe(self, sql: str, *, max_rows: int) -> Rows:
            raise SourceQueryError("the source is gone")

    gate = check_sql(
        "SELECT al.Title FROM Album al JOIN Artist ar ON ar.ArtistId = al.AlbumId",
        PROBED,
        key_columns=keys,
        probing=Probing(ProbeExecutor(Broken()), graph()),
    )

    assert gate.ok, gate.issues
