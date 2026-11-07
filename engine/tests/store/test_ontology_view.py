"""What the Terms and Ontology screens read (BUILD_PLAN.md M5.2, DESIGN.md §5.3, §5.5): the
glossary, one Term, the two layers' graphs and the join path between two Terms. Chinook, compiled
with test_compile.py's scripted gateway and no embedder: its semantic FKs are its ten declared
FKs, Employee.ReportsTo is a self-reference, PlaylistTrack is a junction with no Term and
InvoiceLine one with a Term."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import sql
from fakes import ScriptedGateway
from fastapi.testclient import TestClient
from test_compile import claude
from test_curation import Hashed

from ontological.api.app import create_app
from ontological.catalog.ingest import ingest
from ontological.compile.split import compile_database
from ontological.curate import ontology
from ontological.curate.edits import edit_term, undo
from ontological.curate.ontology import NotFound
from ontological.curate.service import StoreOntology
from ontological.engine.lifetime import Lifetime
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

TOKEN = "t" * 43


@pytest.fixture
def chinook_id(store: pg.Layout, chinook: Path) -> str:
    with SourceExecutor(SqliteSource(chinook)) as executor:
        ingest(executor)
        asyncio.run(compile_database("chinook", executor, ScriptedGateway(claude)))
    return sql(store, "SELECT id FROM catalog_database WHERE name = 'chinook'")


def term_ids(source_id: str) -> dict[str, str]:
    return {t.name: t.id for t in ontology.glossary(source_id).terms}


def test_the_glossary_lists_the_sources_terms_with_their_certification(chinook_id: str) -> None:
    got = ontology.glossary(chinook_id)

    assert got.database_name == "chinook"
    assert [t.name for t in got.terms] == [
        "Album", "Artist", "Customer", "Employee", "Genre", "Invoice", "Invoice Line",
        "Media Type", "Playlist", "Track",
    ]  # fmt: skip
    assert (got.certified, got.total) == (0, 10)
    by_name = {t.name: t for t in got.terms}
    assert by_name["Invoice Line"].kind == "association"
    assert by_name["Track"].kind == "entity"
    assert by_name["Track"].tables == ["Track"]
    assert by_name["Track"].attributes > 0
    assert by_name["Playlist"].sql_attributes == 1  # the bridge SQL through PlaylistTrack
    compiled_at = by_name["Artist"].changed_at
    assert compiled_at is not None

    edit_term(by_name["Artist"].id, name_certified=True, description_certified=True)
    edit_term(by_name["Album"].id, name_certified=True)

    again = ontology.glossary(chinook_id)
    assert (again.certified, again.total) == (1, 10)
    artist = next(t for t in again.terms if t.name == "Artist")
    assert artist.certified and artist.name_certified and artist.description_certified
    assert artist.changed_at is not None and artist.changed_at > compiled_at
    album = next(t for t in again.terms if t.name == "Album")
    assert album.name_certified and not album.certified


def test_an_unknown_source_or_term_is_not_found(chinook_id: str) -> None:
    with pytest.raises(NotFound):
        ontology.glossary("nope")
    with pytest.raises(NotFound):
        ontology.term("nope")
    with pytest.raises(NotFound):
        ontology.path(chinook_id, "nope", term_ids(chinook_id)["Track"])


def test_a_term_has_its_tables_attributes_joins_sql_attributes_and_history(
    chinook_id: str,
) -> None:
    ids = term_ids(chinook_id)

    track = ontology.term(ids["Track"])

    assert [t.name for t in track.tables] == ["Track"]
    assert track.tables[0].database_name == "chinook"
    assert {a.column for a in track.attributes} >= {"Name", "Milliseconds", "UnitPrice"}
    assert [(j.table, j.column, j.to.name) for j in track.joins] == [
        ("Track", "AlbumId", "Album"),
        ("Track", "GenreId", "Genre"),
        ("Track", "MediaTypeId", "Media Type"),
    ]
    assert track.history == [] and track.undoable is None
    playlist = ontology.term(ids["Playlist"])
    [bridge] = playlist.sql_attributes
    assert (bridge.name, bridge.source, bridge.uses) == ("Playlist Tracks", "bridgeTable", 1)
    assert "PlaylistTrack" in (bridge.expression or "")

    renamed = edit_term(ids["Track"], name="Song", synonyms=["tune"])
    assert renamed is not None
    track = ontology.term(ids["Track"])

    assert track.name == "Song" and track.synonyms == ["tune"]
    assert [(c.field, c.old, c.new) for c in track.history] == [
        ("synonyms", [], ["tune"]),
        ("name", "Track", "Song"),
    ]  # newest first; one edit logs its fields in FIELDS order
    assert track.undoable == renamed.id
    undone = undo(renamed.id)
    track = ontology.term(ids["Track"])
    assert track.name == "Track" and track.undoable == undone.id
    assert ("Album", "ArtistId", "Artist") in [
        (j.table, j.column, j.to.name) for j in ontology.term(ids["Album"]).joins
    ]


def test_the_semantic_layer_joins_terms_and_bridges_a_junction_with_no_term(
    chinook_id: str,
) -> None:
    ids = term_ids(chinook_id)
    names = {v: k for k, v in ids.items()}

    got = ontology.graph(chinook_id, "semantic")

    assert [n.name for n in got.nodes] == sorted(ids, key=str.lower)
    edges = {tuple(sorted((names[e.source], names[e.target]))): (e.via, e.join) for e in got.edges}
    assert sorted(edges) == [
        ("Album", "Artist"),
        ("Album", "Track"),
        ("Customer", "Employee"),
        ("Customer", "Invoice"),
        ("Genre", "Track"),
        ("Invoice", "Invoice Line"),
        ("Invoice Line", "Track"),
        ("Media Type", "Track"),
        ("Playlist", "Track"),
    ]
    assert edges[("Album", "Artist")] == (None, "Album.ArtistId = Artist.ArtistId")
    via, join = edges[("Playlist", "Track")]
    assert via == "PlaylistTrack"
    assert join == (
        "PlaylistTrack.PlaylistId = Playlist.PlaylistId; PlaylistTrack.TrackId = Track.TrackId"
    )
    assert not any(n.certified for n in got.nodes)


def test_the_data_layer_draws_every_table_and_its_joins(chinook_id: str) -> None:
    got = ontology.graph(chinook_id, "data")

    assert [n.name for n in got.nodes] == [
        "Album", "Artist", "Customer", "Employee", "Genre", "Invoice", "InvoiceLine",
        "MediaType", "Playlist", "PlaylistTrack", "Track",
    ]  # fmt: skip
    by_name = {n.name: n for n in got.nodes}
    assert by_name["PlaylistTrack"].terms == []
    assert [t.name for t in by_name["InvoiceLine"].terms] == ["Invoice Line"]
    names = {n.id: n.name for n in got.nodes}
    pairs = {frozenset((names[e.source], names[e.target])) for e in got.edges}
    assert pairs == {
        frozenset(p)
        for p in [
            ("Album", "Artist"), ("Customer", "Employee"), ("Customer", "Invoice"),
            ("Invoice", "InvoiceLine"), ("InvoiceLine", "Track"), ("Playlist", "PlaylistTrack"),
            ("PlaylistTrack", "Track"), ("Album", "Track"), ("Genre", "Track"),
            ("MediaType", "Track"),
        ]
    }  # fmt: skip

    edit_term(by_name["Artist"].terms[0].id, name_certified=True, description_certified=True)
    again = {n.name: n for n in ontology.graph(chinook_id, "data").nodes}
    assert again["Artist"].certified and not again["PlaylistTrack"].certified


def test_the_join_path_between_two_terms_follows_the_semantic_layer(chinook_id: str) -> None:
    ids = term_ids(chinook_id)

    got = ontology.path(chinook_id, ids["Customer"], ids["Genre"])

    assert [t.name for t in got.terms] == ["Customer", "Invoice", "Invoice Line", "Track", "Genre"]
    assert [(h.source, h.target) for h in got.hops] == [
        (got.terms[i].id, got.terms[i + 1].id) for i in range(4)
    ]
    assert got.hops[0].join == "Invoice.CustomerId = Customer.CustomerId"
    tables = {
        sql_id: name
        for sql_id, name in ((n.id, n.name) for n in ontology.graph(chinook_id, "data").nodes)
    }
    assert [tables[t] for t in got.tables] == [
        "Customer",
        "Invoice",
        "InvoiceLine",
        "Track",
        "Genre",
    ]
    assert [tables[t] for t in got.hops[0].tables] == ["Customer", "Invoice"]

    through = ontology.path(chinook_id, ids["Artist"], ids["Playlist"])

    assert [t.name for t in through.terms] == ["Artist", "Album", "Track", "Playlist"]
    assert [h.via for h in through.hops] == [None, None, "PlaylistTrack"]
    assert [tables[t] for t in through.hops[-1].tables] == ["Track", "PlaylistTrack", "Playlist"]
    assert [tables[t] for t in through.tables] == [
        "Artist", "Album", "Track", "PlaylistTrack", "Playlist",
    ]  # fmt: skip

    same = ontology.path(chinook_id, ids["Genre"], ids["Genre"])
    assert [t.name for t in same.terms] == ["Genre"] and same.hops == []


# --- through the API --------------------------------------------------------------------------


@pytest.fixture
def api(chinook_id: str) -> Iterator[TestClient]:
    lifetime = Lifetime(idle_timeout_s=600, renew_every_s=30)
    app = create_app(
        token=TOKEN,
        lifetime=lifetime,
        pid=1,
        version="9.9.9",
        ontology=StoreOntology(Hashed()),
    )
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


def test_a_person_renames_certifies_and_undoes_through_the_api(
    api: TestClient, chinook_id: str
) -> None:
    glossary = api.get(f"/sources/{chinook_id}/terms").json()
    ids = {t["name"]: t["id"] for t in glossary["terms"]}

    renamed = api.patch(
        f"/terms/{ids['Track']}",
        json={"name": "  Song ", "synonyms": ["tune", " Tune", "", "number"]},
    )

    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert body["term"]["name"] == "Song"
    assert body["term"]["synonyms"] == ["tune", "number"]
    assert body["reembedded"] > 0  # the Term and its attributes name it
    assert body["term"]["undoable"] == body["edit"]

    certified = api.patch(
        f"/terms/{ids['Track']}", json={"name_certified": True, "description_certified": True}
    ).json()
    assert certified["reembedded"] == 0  # a certification changes no text
    assert api.get(f"/sources/{chinook_id}/terms").json()["certified"] == 1
    assert api.patch(f"/terms/{ids['Track']}", json={"name": "Song"}).json()["edit"] is None

    taken = api.patch(f"/terms/{ids['Track']}", json={"name": "Album"})
    assert taken.status_code == 409
    assert api.patch(f"/terms/{ids['Track']}", json={"name": " "}).status_code == 400
    assert api.patch("/terms/nope", json={"name": "X"}).status_code == 404

    undone = api.post(f"/edits/{certified['edit']}/undo").json()
    assert undone["term"]["name_certified"] is False
    assert api.post(f"/edits/{certified['edit']}/undo").status_code == 409
    assert api.post("/edits/999999/undo").status_code == 404

    graph = api.get(f"/sources/{chinook_id}/graph", params={"layer": "data"}).json()
    assert len(graph["nodes"]) == 11
    path = api.get(
        f"/sources/{chinook_id}/path",
        params={"from_term": ids["Customer"], "to_term": ids["Genre"]},
    ).json()
    assert [t["name"] for t in path["terms"]] == [
        "Customer", "Invoice", "Invoice Line", "Song", "Genre",
    ]  # fmt: skip
    assert api.get("/sources/nope/terms").status_code == 404
    assert api.get(f"/sources/{chinook_id}/graph", params={"layer": "x"}).status_code == 422
