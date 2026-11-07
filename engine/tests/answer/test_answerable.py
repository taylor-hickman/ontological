"""`check_answerable` (BUILD_PLAN.md M4.4) on hits written by hand: the extraction call, the
coverage grade at the distance it is given, and the database it chooses. tests/store/
test_check_answerable.py runs it on real vectors."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from fakes import NoAnswer, ScriptedGateway

from auto_ontology.vdb.postgres import PostgresVDB
from ontological.answer.answerable import (
    PROMPT_VERSION,
    TASK,
    Answerability,
    Extraction,
    check_answerable,
)
from ontological.embed.model import COVERAGE_DISTANCE

VDB = cast(PostgresVDB, object())

# entity -> database -> [(attribute id, distance)], nearest first
ROWS: dict[str, dict[str, list[tuple[str, float]]]] = {
    "artist": {"music": [("a.name", 0.41), ("a.id", 0.55)], "films": [("f.director", 0.60)]},
    "album": {"music": [("al.title", 0.52), ("a.name", 0.70)], "films": [("f.title", 0.66)]},
    "release year": {"music": [("al.year", 0.70)], "films": [("f.year", 0.50)]},
}


def search(
    vdb: PostgresVDB, query: str, *, k: int = 10, where: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    where = where or {}
    rows = [
        (database, id_, d)
        for database, found in ROWS.get(query, {}).items()
        if where.get("database_name") in (None, database)
        for id_, d in found
    ]
    rows.sort(key=lambda r: r[2])
    return [
        {
            "text": id_,
            "metadata": {"id": id_, "label": where["label"], "database_name": database},
            "_distance": d,
        }
        for database, id_, d in rows[:k]
    ]


def contexts(ids: list[str], database: str | None) -> dict[str, dict[str, Any]]:
    return {
        i: {
            "attr_name": i.split(".")[1],
            "term_name": i.split(".")[0],
            "table_name": i.split(".")[0],
            "col_name": i.split(".")[1],
            "database_name": database,
        }
        for i in ids
        if i != "a.id"  # deleted since it was embedded
    }


def extracting(*entities: str) -> ScriptedGateway:
    return ScriptedGateway(
        lambda task, user: {
            "sanitized_question": "Which artists released the most albums?",
            "required_entity_name": list(entities),
        }
    )


def check(gateway: ScriptedGateway, **kw: Any) -> Answerability:
    return asyncio.run(
        check_answerable(
            "Hey, which artists put out the most albums?",
            gateway=gateway,
            vdb=VDB,
            search=search,
            contexts=contexts,
            **kw,
        )
    )


def test_coverage_is_the_fraction_of_entities_with_an_attribute_within_the_distance() -> None:
    got = check(extracting("artist", "album", "release year"), database="music", max_distance=0.6)

    assert got.database == "music"
    assert got.extracted and got.sanitized_question == "Which artists released the most albums?"
    assert got.coverage == round(2 / 3, 4)
    assert got.missing == ["release year"]
    assert got.max_distance == 0.6
    artist, album, year = got.entities
    assert (artist.covered, artist.nearest) == (True, 0.41)
    assert [(a.id, a.attribute, a.term, a.distance) for a in artist.attributes] == [
        ("a.name", "name", "a", 0.41)  # a.id has no context, so it covers nothing
    ]
    assert [a.id for a in album.attributes] == ["al.title"]
    assert (year.covered, year.nearest, year.attributes) == (False, 0.7, [])


def test_the_distance_decides_coverage() -> None:
    entities = ("artist", "album", "release year")
    assert check(extracting(*entities), database="music", max_distance=0.45).coverage == round(
        1 / 3, 4
    )
    assert check(extracting(*entities), database="music", max_distance=0.75).coverage == 1.0


def test_the_default_distance_is_the_default_models() -> None:
    got = check(extracting("artist"), database="music")
    assert got.max_distance == COVERAGE_DISTANCE == 0.671


def test_an_entity_is_covered_by_its_own_distance_only() -> None:
    # "album" reaches a.name at 0.70; "artist" reaches it at 0.41. Upstream's dedupe would score
    # both at 0.41 and cover "album" through it.
    got = check(extracting("album"), database="music", max_distance=0.5)
    assert got.coverage == 0.0
    assert got.entities[0].nearest == 0.52


def test_without_a_database_the_one_covering_most_entities_is_scored() -> None:
    got = check(extracting("artist", "album", "release year"), max_distance=0.6)
    # music has the nearest hits for two entities; films is nearest for one
    assert got.database == "music"
    assert got.missing == ["release year"]


def test_nothing_found_scores_zero_with_every_entity_missing() -> None:
    got = check(extracting("weather", "tide"), max_distance=0.6)
    assert got.database is None
    assert (got.coverage, got.missing) == (0.0, ["weather", "tide"])
    assert [e.nearest for e in got.entities] == [None, None]


def test_the_extraction_is_one_answerable_extract_call() -> None:
    gateway = extracting("artist", " artist ", "", "album")
    got = check(gateway, database="music")

    [call] = gateway.calls
    assert (call.task, call.schema, call.prompt_version) == (TASK, Extraction, PROMPT_VERSION)
    assert "Hey, which artists put out the most albums?" in call.user
    assert "required_entity_name" in call.system
    assert [e.entity for e in got.entities] == ["artist", "album"]  # stripped, deduplicated


def test_a_failed_extraction_lets_the_question_stand_in() -> None:
    for said in (None, NoAnswer()):
        got = check(ScriptedGateway(lambda task, user, said=said: said), database="music")
        assert not got.extracted
        assert got.sanitized_question == got.question
        assert [e.entity for e in got.entities] == [got.question]


def test_no_entities_lets_the_sanitized_question_stand_in() -> None:
    got = check(extracting(), database="music")
    assert got.extracted
    assert [e.entity for e in got.entities] == ["Which artists released the most albums?"]
