"""The answer agent on Chinook (BUILD_PLAN.md M1.7): the tools reading the real store through
`StoreOntology`, and a scripted session that plans, fails and passes the gate, runs its SQL on the
Chinook source and is reviewed. Chinook is compiled with test_compile.py's scripted gateway and
embedded by the real model, as in test_grounding.py. Needs the model and the binaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import ScriptedGateway
from llm.sdk_fakes import (
    OPUS,
    SONNET,
    Call,
    FakeQuery,
    MemoryCalls,
    fake_claude,
    init,
    limit,
    result,
    said,
)
from llm.sdk_fakes import answered as structured_session
from test_compile import claude
from test_grounding import compile_source

from auto_ontology.vdb.postgres import PostgresVDB
from ontological.answer.agent import ask
from ontological.answer.gate import Probing, check_sql
from ontological.answer.ontology import StoreOntology
from ontological.answer.pack import build_pack
from ontological.answer.probe import ProbeExecutor
from ontological.compile.split import Compiled
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import semantic_vdb
from ontological.llm.gateway import ClaudeGateway
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

QUESTION = "How many albums does AC/DC have?"
SQL = (
    "SELECT ar.Name, COUNT(al.AlbumId) AS albums FROM Artist ar "
    "JOIN Album al ON al.ArtistId = ar.ArtistId WHERE ar.Name = 'AC/DC' GROUP BY ar.Name"
)


@pytest.fixture
def vdb(embedder: LocalEmbedder) -> Iterator[PostgresVDB]:
    v = semantic_vdb(embedder)
    try:
        yield v
    finally:
        v.close()


@pytest.fixture
def compiled(store: pg.Layout, chinook: Path, embedder: LocalEmbedder) -> Compiled:
    return compile_source(chinook, embedder, ScriptedGateway(claude))


@pytest.fixture
def ontology(compiled: Compiled, vdb: PostgresVDB) -> StoreOntology:
    assert compiled.complete
    return StoreOntology(vdb)


def gate(ontology: StoreOntology, sql: str) -> list[str]:
    return check_sql(
        sql,
        ontology.catalog("chinook"),
        key_columns=lambda table: ontology.key_columns("chinook", table),
    ).issues


def test_the_gate_reads_chinooks_catalog_and_keys(ontology: StoreOntology) -> None:
    passed = check_sql(
        SQL,
        ontology.catalog("chinook"),
        key_columns=lambda table: ontology.key_columns("chinook", table),
    )
    assert passed.ok
    assert [t.split(".")[-1] for t in passed.tables] == ["Album", "Artist"]
    assert "Artist.Name" in passed.columns  # spelled as the catalog spells it

    [unknown] = gate(ontology, "SELECT Titel FROM Album")
    assert "titel" in unknown.lower() and "Title" in unknown
    assert ontology.key_columns("chinook", "Album").pk == ["albumid"]
    # The keys say Track is Album's many side
    [unsafe] = gate(
        ontology,
        "SELECT al.Title, t.Milliseconds FROM Album al JOIN Track t ON t.AlbumId = al.AlbumId "
        "GROUP BY al.AlbumId, al.Title",
    )
    assert "t.Milliseconds" in unsafe


def test_checks_4_to_6_read_chinooks_graph_and_probe_its_source(
    ontology: StoreOntology, chinook: Path
) -> None:
    catalog = ontology.catalog("chinook")
    album = catalog.table(None, "album")
    assert album is not None and album.types["title"]  # the gate's known types
    graph = ontology.join_graph("chinook")
    track_album = graph.column_id("Track", "AlbumId")
    album_id = graph.column_id("album", "albumid")  # any case, as the SQL writes it
    assert track_album and album_id and graph.in_semantic_fk(album_id)
    [hop] = graph.join_path(track_album, album_id)
    assert (hop["source_table"], hop["target_table"]) == ("Track", "Album")

    with SourceExecutor(SqliteSource(chinook)) as executor:

        def issues(sql: str) -> list[str]:
            return check_sql(
                sql,
                catalog,
                key_columns=lambda table: ontology.key_columns("chinook", table),
                probing=Probing(ProbeExecutor(executor), graph),
            ).issues

        assert issues(SQL) == []
        [join] = issues("SELECT t.Name FROM Track t JOIN Album al ON t.TrackId = al.AlbumId")
        assert "Track.TrackId = Album.AlbumId" in join
        [literal] = issues("SELECT Name FROM Genre WHERE Name = 'rock'")
        assert "Closest real value(s): Rock." in literal


def test_search_and_describe_read_the_semantic_layer_and_the_catalog(
    ontology: StoreOntology,
) -> None:
    hits = ontology.search("chinook", "artist name", ["attribute", "term"])
    assert hits and {h["label"] for h in hits} <= {"ColumnAttribute", "Term"}
    assert [h["score"] for h in hits] == sorted(h["score"] for h in hits)
    assert {h["database_name"] for h in hits} == {"chinook"}

    by_name = ontology.describe_table("chinook", "Artist")
    assert by_name is not None and by_name["name"] == "Artist"
    assert "Name" in [c["name"] for c in by_name["columns"]]
    assert ontology.describe_table("chinook", by_name["id"]) == by_name
    assert ontology.describe_table("chinook", "Nope") is None
    assert ontology.describe_table("garden", "Artist") is None


def test_a_scripted_session_answers_from_chinook_and_is_verified(
    ontology: StoreOntology, vdb: PostgresVDB, chinook: Path, tmp_path: Path
) -> None:
    pack = build_pack(vdb, QUESTION, database="chinook")
    answer = {
        "answer": "AC/DC has two albums.",
        "sql": SQL,
        "plan_id": "plan-1",
        "validation_id": "val-1",
        "review_id": "rev-1",
        "answerable": True,
    }
    agent = [
        init(OPUS),
        limit(),
        Call("plan", {"intent": "calculation", "calc_subtype": "counting", "entities": ["AC/DC"]}),
        Call("describe_table", {"id": "Album"}),
        Call("validate_sql", {"sql": "SELECT COUNT(*) FROM Albums"}),
        Call("validate_sql", {"sql": SQL}),
        Call("run_sql", {"validation_id": "val-1"}),
        Call("review_answer", {"validation_id": "val-1", "answer_draft": answer["answer"]}),
        said("Done.", model=OPUS),
        result(structured=answer),
    ]
    review = structured_session(
        {"reasoning": "Counts AC/DC's albums.", "is_valid": True}, model=SONNET
    )
    query = FakeQuery([agent, review])
    gateway = ClaudeGateway(
        calls=MemoryCalls(),
        sandbox=tmp_path / "claude-sandbox",
        cli_path=fake_claude(tmp_path),
        query=query,
    )

    with SourceExecutor(SqliteSource(chinook)) as executor:

        async def go() -> Any:
            return await ask(
                QUESTION, pack, gateway=gateway, ontology=ontology, runner=executor
            ).answered()

        got = asyncio.run(go())

    assert got.verified, got.reason
    log = {c.name: c for c in got.tool_log}
    assert log["describe_table"].data["table"]["name"] == "Album"
    failed = got.tool_log[2]
    assert failed.is_error and "Albums" in failed.text and "Album" in failed.text
    assert log["run_sql"].data["rows"] == [["AC/DC", 2]]
    assert json.loads(query.results[4]["content"][0]["text"])["rows"] == [["AC/DC", 2]]
    assert "AC/DC" in query.spawns[1].prompt  # the critic saw the row
