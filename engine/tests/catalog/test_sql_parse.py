"""Query-history parsing and dedup (BUILD_PLAN.md M2.4): upstream's `parse_query_slim` and its
duplicate rules, over a `CatalogIndex`, in-process and in the parse pool's spawned workers."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ontological.catalog.parse_pool import ParsePool, chunked
from ontological.catalog.parsers.catalog_index import CatalogIndex, SchemaEntry, TableEntry
from ontological.catalog.sql_parse import Link, normalize, parse_chunk, parse_query

DIALECTS = ["sqlite"]

ALBUM = {"albumid": "c-al-id", "title": "c-al-title", "artistid": "c-al-artist"}
ARTIST = {"artistid": "c-ar-id", "name": "c-ar-name"}
SALES_ARTIST = {"artistid": "c-s-id", "region": "c-s-region"}
CATALOG = CatalogIndex(
    {
        "main": SchemaEntry(
            {
                "album": TableEntry("t-album", "Album", ALBUM),
                "artist": TableEntry("t-artist", "Artist", ARTIST),
            }
        ),
        "sales": SchemaEntry({"artist": TableEntry("t-s-artist", "Artist", SALES_ARTIST)}),
    }
)

JOIN = (
    "SELECT a.Title, r.Name FROM Album a JOIN Artist r ON a.ArtistId = r.ArtistId "
    "WHERE r.Name = 'AC/DC'"
)


def test_a_statement_resolves_to_catalog_ids() -> None:
    q = parse_query(JOIN, CATALOG, DIALECTS)

    assert q is not None
    assert q.texts == [JOIN]
    assert q.table_ids == {"t-album", "t-artist"}  # unqualified: the first schema holding it
    assert q.column_ids == {"c-al-title", "c-al-artist", "c-ar-id", "c-ar-name"}
    assert q.joins == [Link("c-al-artist", "c-ar-id", "album.artistid = artist.artistid")]
    assert (q.join_count, q.union_count, q.unions) == (1, 0, [])
    assert q.nodes_count > 0
    assert q.normalized == normalize(JOIN, DIALECTS)


def test_a_schema_prefix_picks_that_schema_s_table() -> None:
    q = parse_query("SELECT region FROM sales.artist WHERE artistid = 1", CATALOG, DIALECTS)

    assert q is not None
    assert (q.table_ids, q.column_ids) == ({"t-s-artist"}, {"c-s-region", "c-s-id"})


def test_a_union_pairs_the_columns_in_each_position() -> None:
    q = parse_query("SELECT Name FROM Artist UNION SELECT Title FROM Album", CATALOG, DIALECTS)

    assert q is not None
    assert q.unions == [Link("c-al-title", "c-ar-name", "album.title | artist.name")]


def test_other_literals_or_select_order_fold_into_one_statement() -> None:
    same = [
        JOIN,
        JOIN.replace("AC/DC", "Queen"),
        JOIN.replace("a.Title, r.Name", "r.Name, a.Title"),
    ]
    other = JOIN.replace("r.Name = 'AC/DC'", "r.ArtistId = 1")  # another column

    got = parse_chunk([*same, other], CATALOG, DIALECTS)

    assert [q.texts for q in got.parsed] == [same, [other]]
    assert (got.unmatched, got.failed) == (0, 0)


def test_a_statement_on_no_catalog_table_is_unmatched_and_bad_sql_fails() -> None:
    got = parse_chunk(["SELECT x FROM elsewhere", "SELEC oops FROM"], CATALOG, DIALECTS)

    assert (got.parsed, got.unmatched, got.failed) == ([], 1, 1)
    assert got.failures[0][0] == "SELEC oops FROM"
    assert got.failures[0][1].startswith("SQLSyntaxError")


def test_chunks_spread_over_the_workers_and_stop_at_500() -> None:
    assert [len(c) for c in chunked(list(range(10)), 4)] == [3, 3, 3, 1]
    assert [len(c) for c in chunked(list(range(1200)), 2)] == [500, 500, 200]
    assert chunked([], 4) == []


def test_the_pool_parses_as_the_engine_would() -> None:
    texts = [JOIN, JOIN.replace("AC/DC", "Queen"), "SELECT Name FROM Artist", "SELECT 1 FROM nope"]
    with ParsePool(workers=2) as pool:
        got = list(pool.parse(texts, CATALOG, DIALECTS))
        normalized = pool.normalize(texts, DIALECTS)

    assert [q for chunk in got for q in chunk.parsed] == (
        parse_chunk(texts[:2], CATALOG, DIALECTS).parsed
        + parse_chunk(texts[2:], CATALOG, DIALECTS).parsed
    )
    assert sum(c.unmatched for c in got) == 1
    assert normalized == [normalize(t, DIALECTS) for t in texts]


@pytest.mark.parametrize("module", ["ontological.catalog.sql_parse"])
def test_a_worker_loads_nothing_that_reaches_the_store(module: str) -> None:
    """Spawned workers import the module their task lives in, and nothing of the store's."""
    script = f"import json, sys; import {module}\nprint(json.dumps(sorted(sys.modules)))\n"
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    loaded = json.loads(out.stdout)
    reach = [
        m
        for m in loaded
        if m.split(".")[0] in {"psycopg", "sqlalchemy", "auto_ontology"}
        or m.startswith("ontological.store")
    ]
    assert reach == []
