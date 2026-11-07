"""The answer agent on Chinook (BUILD_PLAN.md M1.7, M1.8, M4.3): the tools reading the real store
through `StoreOntology`, and scripted sessions, run as `POST /ask` runs them (`StoreAnswers`),
that plan, fail and pass the gate, run their SQL on the Chinook source found in the catalog and
are reviewed, with Chinook named and without. Chinook is compiled with test_compile.py's scripted
gateway and embedded by the real model, as in test_grounding.py. Needs the model and the
binaries."""

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
from ontological.answer.gate import Probing, check_sql
from ontological.answer.ontology import StoreOntology
from ontological.answer.probe import ProbeExecutor
from ontological.answer.service import (
    Asked,
    NoSource,
    RanSql,
    StoreAnswers,
    TermHit,
    Validated,
)
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
        # MediaType has 5 names; Genre's 25 are too many to judge before the query runs
        [literal] = issues("SELECT MediaTypeId FROM MediaType WHERE Name = 'mpeg audio file'")
        assert "Closest real value(s): MPEG audio file." in literal
        assert issues("SELECT GenreId FROM Genre WHERE Name = 'rock'") == []


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


def test_plan_pack_get_term_and_join_path_read_chinook(ontology: StoreOntology) -> None:
    pack = ontology.plan_pack(
        "chinook",
        QUESTION,
        entities=["album title", "artist name"],
        evidence="",
        subject="artist",
        anchor_attribute_id=None,
    )
    assert pack.database == "chinook" and pack.attributes and pack.anchor is not None
    assert {a["database_name"] for a in pack.attributes} == {"chinook"}
    entities = {e for a in pack.attributes for e in a.get("query_entities", [])}
    assert entities == {"album title", "artist name"}

    term = ontology.get_term("chinook", "artist")  # by name, in any case
    assert term is not None and term["name"] == "Artist"
    assert ontology.get_term("chinook", term["id"]) == term
    assert term["tables"] == ["Artist"]
    assert "Artist.Name" in [a["column"] for a in term["attributes"]]
    assert ontology.get_term("chinook", "Nope") is None
    assert ontology.get_term("garden", "Artist") is None

    joined = {"Album.ArtistId", "Artist.ArtistId"}
    for start, end in (("Album", "Artist"), ("Artist", "Album")):
        path = ontology.join_path("chinook", start, end)
        assert path is not None and len(path) == 1, (start, end, path)
        [hop] = path
        ends = {f"{hop['source_table']}.{hop['source_column']}"}
        ends.add(f"{hop['target_table']}.{hop['target_column']}")
        assert ends == joined
    assert ontology.join_path("chinook", "Album", "Nope") is None


def session(answer: dict[str, Any], *calls: Call) -> list[Any]:
    return [init(OPUS), limit(), *calls, said("Done.", model=OPUS), result(structured=answer)]


def scripted(tmp_path: Path, *scripts: list[Any]) -> tuple[FakeQuery, ClaudeGateway]:
    query = FakeQuery(list(scripts))
    gateway = ClaudeGateway(
        calls=MemoryCalls(),
        sandbox=tmp_path / "claude-sandbox",
        cli_path=fake_claude(tmp_path),
        query=query,
    )
    return query, gateway


def test_a_scripted_session_answers_from_chinook_and_is_verified(
    ontology: StoreOntology,
    embedder: LocalEmbedder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    closed: list[bool] = []

    def open_vdb(e: LocalEmbedder) -> PostgresVDB:
        vdb = semantic_vdb(e)
        close = vdb.close
        monkeypatch.setattr(vdb, "close", lambda: (closed.append(True), close())[1])
        return vdb

    answers = StoreAnswers(gateway=gateway, embedder=embedder, open_vdb=open_vdb)
    try:
        got: Asked = asyncio.run(answers.ask(QUESTION, "chinook"))
        assert closed == [], "the engine's VDB stays open for the next question"
    finally:
        answers.close()
    assert closed == [True], "closed at shutdown, its pool disposed"

    assert got.verified, got.reason
    assert "Artist" in str(query.spawns[0].options.system_prompt)  # the pack, from the store
    assert got.sql == SQL
    assert got.result is not None
    assert (got.result.columns, got.result.rows) == (["Name", "albums"], [["AC/DC", 2]])
    log = {c.name: c for c in got.steps}
    assert log["describe_table"].data["table"]["name"] == "Album"
    failed = got.steps[2]
    assert failed.is_error and "Albums" in failed.text and "Album" in failed.text
    assert log["run_sql"].data["rows"] == [["AC/DC", 2]]
    assert json.loads(query.results[4]["content"][0]["text"])["rows"] == [["AC/DC", 2]]
    assert "AC/DC" in query.spawns[1].prompt  # the critic saw the row


def test_without_a_database_the_question_is_answered_from_the_one_that_covers_it(
    ontology: StoreOntology, embedder: LocalEmbedder, tmp_path: Path
) -> None:
    answer = {
        "answer": "AC/DC has two albums.",
        "sql": SQL,
        "plan_id": "plan-1",
        "validation_id": "val-1",
        "review_id": "rev-1",
        "answerable": True,
    }
    calls = [
        Call("plan", {"intent": "calculation", "calc_subtype": "counting", "entities": ["AC/DC"]}),
        Call("validate_sql", {"sql": SQL}),
        Call("run_sql", {"validation_id": "val-1"}),
        Call("review_answer", {"validation_id": "val-1", "answer_draft": answer["answer"]}),
    ]
    review = structured_session({"reasoning": "Counts albums.", "is_valid": True}, model=SONNET)
    query, gateway = scripted(tmp_path, session(answer, *calls), review)
    answers = StoreAnswers(gateway=gateway, embedder=embedder)
    try:
        got = asyncio.run(answers.ask("How many albums does each artist have?", None))
    finally:
        answers.close()

    assert got.verified, got.reason
    assert got.database == "chinook"
    system = str(query.spawns[0].options.system_prompt)
    assert "ranked them by how much of it each covers" in system and "- chinook:" in system
    plan = got.steps[0]
    assert plan.data["database"] == "chinook" and plan.data["attribute_ids"]
    assert "Candidates for the plan's entities" in plan.text
    assert "## The counting template" in plan.text


def test_the_shims_gate_and_term_search_read_chinook(
    ontology: StoreOntology, embedder: LocalEmbedder, tmp_path: Path
) -> None:
    # M6.2: validate_sql and run_validated_sql with no session, and search_terms; no Claude
    _, gateway = scripted(tmp_path)
    answers = StoreAnswers(gateway=gateway, embedder=embedder)

    async def go() -> tuple[Validated, Validated, RanSql, RanSql, list[TermHit]]:
        bad = await answers.validate_sql("SELECT COUNT(*) FROM Albums", "chinook")
        good = await answers.validate_sql(SQL, "chinook")
        assert good.validation_id is not None
        ran = await answers.run_sql(good.validation_id)
        # 275 names: more than the gate judges, so 'ac/dc' passes and comes back empty
        near_miss = await answers.validate_sql(SQL.replace("'AC/DC'", "'ac/dc'"), "chinook")
        assert near_miss.validation_id is not None, near_miss.issues
        empty = await answers.run_sql(near_miss.validation_id)
        return bad, good, ran, empty, await answers.search_terms("artist", "chinook")

    try:
        bad, good, ran, empty, terms = asyncio.run(go())
        with pytest.raises(NoSource):
            asyncio.run(answers.validate_sql(SQL, "garden"))
    finally:
        answers.close()

    assert not bad.ok and bad.validation_id is None and "Albums" in bad.issues[0]
    assert good.ok and {"main.Artist", "main.Album"} <= set(good.tables)
    assert (ran.outcome, ran.columns, ran.rows) == ("rows", ["Name", "albums"], [["AC/DC", 2]])
    assert empty.outcome == "empty" and "AC/DC" in empty.repairs
    assert empty.note is not None and "no rows" in empty.note
    assert terms and terms[0].name == "Artist" and {t.database for t in terms} == {"chinook"}
