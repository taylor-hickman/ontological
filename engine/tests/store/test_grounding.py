"""The semantic layer's vectors and the grounding pack (BUILD_PLAN.md M1.6) on Chinook: compiled
with the scripted gateway of test_compile.py, embedded by the real model into the bundled store,
then searched, with join paths from the vendored `find_join_path`. Needs the model and the
binaries."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import sql
from fakes import NoAnswer, ScriptedGateway
from test_compile import claude, table_of

from auto_ontology.vdb.postgres import PostgresVDB, on_change
from ontological.answer.pack import QUESTION_K, build_pack
from ontological.catalog.ingest import ingest
from ontological.compile.split import Compiled, compile_database
from ontological.embed import model
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import dense_search, semantic_vdb
from ontological.embed.semantic import Synced, sync_database
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

GARDEN = """
CREATE TABLE Plant (PlantId INTEGER PRIMARY KEY, Species TEXT, Sunlight TEXT);
CREATE TABLE Bed (
  BedId INTEGER PRIMARY KEY, PlantId INTEGER REFERENCES Plant (PlantId), PlantedOn TEXT
);
INSERT INTO Plant VALUES (1, 'Tomato', 'full sun'), (2, 'Fern', 'shade'), (3, 'Basil', 'full sun');
INSERT INTO Bed VALUES (1, 1, '2026-04-01'), (2, 2, '2026-04-02'), (3, 3, '2026-05-10');
"""


def compile_source(path: Path, embedder: LocalEmbedder, gateway: ScriptedGateway) -> Compiled:
    with SourceExecutor(SqliteSource(path)) as executor:
        name = ingest(executor).database_name
        return asyncio.run(compile_database(name, executor, gateway, embedder=embedder))


@pytest.fixture
def vdb(embedder: LocalEmbedder) -> Iterator[PostgresVDB]:
    v = semantic_vdb(embedder)
    try:
        yield v
    finally:
        v.close()


@pytest.fixture
def chinook_compiled(store: pg.Layout, chinook: Path, embedder: LocalEmbedder) -> Compiled:
    return compile_source(chinook, embedder, ScriptedGateway(claude))


def rows(lay: pg.Layout, q: str) -> list[str]:
    out = sql(lay, q)
    return out.splitlines() if out else []


def place(attribute: dict[str, Any]) -> str:
    return f"{attribute['table_name']}.{attribute['col_name']}"


def hops(path: list[dict[str, str]]) -> list[str]:
    return [
        f"{h['source_table']}.{h['source_column']} -> {h['target_table']}.{h['target_column']}"
        for h in path
    ]


# --- the semantic layer ---------------------------------------------------------------------


def test_the_compile_embeds_each_term_and_attribute(
    chinook_compiled: Compiled, store: pg.Layout
) -> None:
    terms = int(sql(store, "SELECT count(*) FROM term"))
    attributes = int(sql(store, "SELECT count(*) FROM column__has_attribute"))
    # and PlaylistTrack's bridge, Chinook's one SQL attribute
    assert chinook_compiled.vectors == Synced(embedded=terms + attributes + 1, deleted=0, current=0)

    assert rows(
        store, "SELECT label || ' ' || count(*) FROM vdb.semantic_layer GROUP BY label ORDER BY 1"
    ) == [f"ColumnAttribute {attributes}", "SqlAttribute 1", f"Term {terms}"]
    assert rows(store, "SELECT DISTINCT database_name FROM vdb.semantic_layer") == ["chinook"]
    # Upstream's texts; every vector names its model and the hash of its text
    [country] = rows(
        store,
        "SELECT content || '|' || (langchain_metadata ->> 'model') FROM vdb.semantic_layer "
        "WHERE langchain_metadata ->> 'table_name' = 'Invoice' "
        "AND langchain_metadata ->> 'source_column' = 'BillingCountry'",
    )
    assert country == (
        "ColumnAttribute: Billing Country of Term Invoice. About BillingCountry. "
        f"Sample values: USA, Canada, France, Brazil, Germany.|{model.MODEL_ID}"
    )
    assert rows(
        store,
        "SELECT content FROM vdb.semantic_layer WHERE content LIKE 'Term: Album.%'",
    ) == ["Term: Album. A Album."]


def test_a_sync_embeds_only_what_changed_and_drops_what_is_gone(
    chinook_compiled: Compiled, store: pg.Layout, vdb: PostgresVDB
) -> None:
    assert chinook_compiled.vectors is not None
    total = chinook_compiled.vectors.embedded
    assert sync_database(vdb, "chinook") == Synced(embedded=0, deleted=0, current=total)

    genre = int(  # the Term's row and its attributes'
        sql(
            store,
            "SELECT 1 + count(*) FROM column_attribute__term at JOIN term ON term.id = at.term_id "
            "WHERE term.name = 'Genre'",
        )
    )
    sql(store, "DELETE FROM term WHERE name = 'Genre'")
    heard: list[tuple[str, str]] = []
    stop = on_change(lambda schema, collection: heard.append((schema, collection)))
    try:
        # Nothing to embed: the delete alone tells the listeners, so BM25 rebuilds (M3.6)
        assert sync_database(vdb, "chinook") == Synced(
            embedded=0, deleted=genre, current=total - genre
        )
    finally:
        stop()
    assert heard == [("vdb", "semantic_layer")]

    sql(store, "UPDATE term SET description = 'One sale.' WHERE name = 'Invoice'")
    assert sync_database(vdb, "chinook") == Synced(embedded=1, deleted=1, current=total - genre - 1)
    assert rows(
        store,
        "SELECT content FROM vdb.semantic_layer WHERE content LIKE 'Term: Invoice.%'",
    ) == ["Term: Invoice. One sale."]
    assert rows(
        store,
        "SELECT count(*) FROM vdb.semantic_layer WHERE langchain_metadata ->> 'name' = 'Genre'",
    ) == ["0"]


def test_a_stopped_compile_embeds_the_tables_it_wrote(
    store: pg.Layout, chinook: Path, embedder: LocalEmbedder
) -> None:
    def logged_out_at_employee(task: str, user: str) -> dict[str, Any] | NoAnswer | None:
        if task == "table_terms" and table_of(user) == "Employee":
            return NoAnswer("error", "needs_login")  # a wait won't fix it: the job stops
        return claude(task, user)

    stopped = compile_source(chinook, embedder, ScriptedGateway(logged_out_at_employee))

    assert stopped.stopped is not None
    term_rows = "SELECT langchain_metadata ->> 'name' FROM vdb.semantic_layer WHERE label = 'Term'"
    # The small tables' pack and Customer were written before Employee
    assert rows(store, term_rows + " ORDER BY 1") == [
        "Album", "Artist", "Customer", "Genre", "Invoice Line", "Media Type", "Playlist",
    ]  # fmt: skip

    resumed = compile_source(chinook, embedder, ScriptedGateway(claude))

    assert stopped.vectors is not None and resumed.complete and resumed.vectors is not None
    assert resumed.vectors.current == stopped.vectors.embedded  # none embedded twice
    assert len(rows(store, term_rows)) == 10


# --- the grounding pack ---------------------------------------------------------------------


def test_the_pack_for_a_question_within_a_scope(
    chinook_compiled: Compiled, vdb: PostgresVDB
) -> None:
    pack = build_pack(vdb, "which country is each invoice billed to", database="chinook")

    assert pack.database == "chinook" and pack.coverage == []
    assert len(pack.attributes) == QUESTION_K
    assert place(pack.attributes[0]) == "Invoice.BillingCountry"
    assert [a["score"] for a in pack.attributes] == sorted(a["score"] for a in pack.attributes)
    assert {a["query_entities"][0] for a in pack.attributes} == {
        "which country is each invoice billed to"
    }
    assert [t["name"] for t in pack.terms] == ["Invoice"]
    assert pack.tables[0]["name"] == "Invoice"
    assert "BillingCountry" in [c["name"] for c in pack.tables[0]["columns"]]

    assert pack.anchor is not None and place(pack.anchor) == "Invoice.BillingCountry"
    paths = {place(j): hops(j["path"]) for j in pack.join_paths}
    assert paths["Invoice.BillingCity"] == []  # the same table
    assert paths["Customer.Country"] == ["Invoice.CustomerId -> Customer.CustomerId"]
    assert paths["InvoiceLine.InvoiceLineId"] == ["Invoice.InvoiceId -> InvoiceLine.InvoiceId"]


def test_the_pack_runs_on_the_search_it_is_given(
    chinook_compiled: Compiled, vdb: PostgresVDB
) -> None:
    """The retrieval benchmark's seam (§7.4): every search the pack makes goes through `search`,
    and `question_k` sets how many attributes the raw question takes."""
    calls: list[tuple[str, int, dict[str, Any] | None]] = []

    def search(
        vdb: PostgresVDB, query: str, *, k: int = 10, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        calls.append((query, k, where))
        return dense_search(vdb, query, k=k, where=where)

    question = "which country is each invoice billed to"
    pack = build_pack(
        vdb,
        question,
        evidence="billed means invoiced",
        database="chinook",
        question_k=4,
        search=search,
    )
    assert len(pack.attributes) <= 4 + 3
    assert [(q, k, (w or {}).get("label")) for q, k, w in calls] == [
        (question, 4, "ColumnAttribute"),
        ("billed means invoiced", 3, "ColumnAttribute"),
        (question, 3, "SqlAttribute"),  # M4.3: the question searches the SQL attributes
        (question, 1, "Term"),
    ]
    assert all((w or {}).get("database_name") == "chinook" for _, _, w in calls)


def test_the_plan_pack_searches_each_entity_and_joins_from_the_given_anchor(
    chinook_compiled: Compiled, vdb: PostgresVDB
) -> None:
    question = "genres of the tracks each artist recorded"
    entities = ["artist name", "genre name"]
    first = build_pack(vdb, question, entities=entities, subject="track", database="chinook")
    by_place = {place(a): a for a in first.attributes}
    assert {e for a in first.attributes for e in a["query_entities"]} == set(entities)
    assert len(first.attributes) <= 2 * len(entities)
    assert [t["name"] for t in first.terms] == ["Track"]

    pack = build_pack(
        vdb,
        question,
        entities=entities,
        subject="track",
        database="chinook",
        anchor_attribute_id=by_place["Artist.Name"]["id"],
    )

    assert pack.anchor is not None and place(pack.anchor) == "Artist.Name"
    paths = {place(j): hops(j["path"]) for j in pack.join_paths}
    assert paths["Genre.Name"] == [
        "Artist.ArtistId -> Album.ArtistId",
        "Album.AlbumId -> Track.AlbumId",
        "Track.GenreId -> Genre.GenreId",
    ]


def test_without_a_scope_the_database_covering_most_entities_is_chosen(
    chinook_compiled: Compiled,
    chinook: Path,
    embedder: LocalEmbedder,
    vdb: PostgresVDB,
) -> None:
    garden = chinook.with_name("garden.sqlite")
    con = sqlite3.connect(garden)
    try:
        con.executescript(GARDEN)
    finally:
        con.close()
    assert compile_source(garden, embedder, ScriptedGateway(claude)).complete

    pack = build_pack(vdb, "which plant species need full sun")
    assert pack.database == "garden"
    assert {a["database_name"] for a in pack.attributes} == {"garden"}
    assert pack.coverage[0].database == "garden"
    assert {t["name"] for t in pack.tables} <= {"Plant", "Bed"}

    # Chinook covers two of the three entities. The third searches Chinook again, alone.
    entities = ["invoice billing country", "invoice total", "plant sunlight"]
    pack = build_pack(vdb, "sales by plant", entities=entities)
    assert pack.database == "chinook"
    assert pack.coverage[0].database == "chinook"
    assert {a["database_name"] for a in pack.attributes} == {"chinook"}
    assert "plant sunlight" in {e for a in pack.attributes for e in a["query_entities"]}
