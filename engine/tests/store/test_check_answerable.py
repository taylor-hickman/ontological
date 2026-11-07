"""`check_answerable` (BUILD_PLAN.md M4.4) on real vectors: Chinook and a small garden database,
compiled with test_compile.py's scripted gateway and embedded by the real model, as in
test_grounding.py, then checked through `StoreAnswers` at the model's coverage distance. The
extraction is scripted. Needs the model and the binaries."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from fakes import ScriptedGateway
from test_compile import claude
from test_grounding import GARDEN, compile_source

from ontological.answer.answerable import TASK, Answerability
from ontological.answer.service import StoreAnswers
from ontological.embed import model
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import semantic_vdb
from ontological.llm.gateway import ClaudeGateway
from ontological.store import pg

ENTITIES = {
    "Which country is each invoice billed to?": ["invoice", "invoice billing country"],
    "Which plant species need full sun?": ["plant species", "plant sunlight"],
}


def extraction(task: str, user: str) -> dict[str, Any]:
    assert task == TASK
    [question] = [q for q in ENTITIES if q in user]
    return {"sanitized_question": question, "required_entity_name": ENTITIES[question]}


@pytest.fixture
def compiled(store: pg.Layout, chinook: Path, embedder: LocalEmbedder, tmp_path: Path) -> None:
    garden = tmp_path / "garden.sqlite"
    con = sqlite3.connect(garden)
    try:
        con.executescript(GARDEN)
    finally:
        con.close()
    for path in (chinook, garden):
        assert compile_source(path, embedder, ScriptedGateway(claude)).complete


def check(embedder: LocalEmbedder, question: str, database: str | None = None) -> Answerability:
    answers = StoreAnswers(
        gateway=cast(ClaudeGateway, ScriptedGateway(extraction)),
        embedder=embedder,
        open_vdb=semantic_vdb,
    )
    return asyncio.run(answers.check(question, database))


def test_a_question_its_database_covers(compiled: None, embedder: LocalEmbedder) -> None:
    got = check(embedder, "Which country is each invoice billed to?", "chinook")

    assert got.max_distance == model.DEFAULT.coverage_distance
    assert (got.database, got.coverage, got.missing) == ("chinook", 1.0, [])
    country = got.entities[1]
    assert (country.attributes[0].table, country.attributes[0].column) == (
        "Invoice",
        "BillingCountry",
    )
    assert all(a.distance <= got.max_distance for e in got.entities for a in e.attributes)


def test_the_same_question_against_the_wrong_database(
    compiled: None, embedder: LocalEmbedder
) -> None:
    got = check(embedder, "Which country is each invoice billed to?", "garden")

    assert got.database == "garden"
    assert (got.coverage, got.missing) == (0.0, ["invoice", "invoice billing country"])
    assert all(e.nearest is not None and e.nearest > got.max_distance for e in got.entities)


def test_without_a_database_the_covering_one_is_chosen(
    compiled: None, embedder: LocalEmbedder
) -> None:
    got = check(embedder, "Which plant species need full sun?")

    assert (got.database, got.coverage) == ("garden", 1.0)
    assert {a.table for e in got.entities for a in e.attributes} == {"Plant"}
