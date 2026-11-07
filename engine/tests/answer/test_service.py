"""`StoreAnswers` (BUILD_PLAN.md M1.8) closes the question's semantic VDB, which holds a connection
pool and a loop thread, whether the question is answered or fails. tests/store/test_answer.py
answers one on the real store."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from fakes import ScriptedGateway

from auto_ontology.vdb.postgres import PostgresVDB
from ontological.answer import service
from ontological.answer.service import NoEmbedder, NoSource, StoreAnswers
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.model import BGE_SMALL
from ontological.llm.gateway import ClaudeGateway
from ontological.sources.executor import SourceExecutor
from ontological.sources.spec import Source, SqliteSource

EMBEDDER = cast(LocalEmbedder, object())
GATEWAY = cast(ClaudeGateway, object())


class DownVdb:
    """A VDB whose store is down: anything but close() raises."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("the store is down")


class Opened:
    def __init__(self) -> None:
        self.vdbs: list[DownVdb] = []
        self.executors: list[Source] = []

    def vdb(self, embedder: LocalEmbedder) -> PostgresVDB:
        self.vdbs.append(DownVdb())
        return cast(PostgresVDB, self.vdbs[-1])

    def executor(self, source: Source) -> SourceExecutor:
        self.executors.append(source)
        raise AssertionError("no executor before the pack is built")


def answers(opened: Opened, *, source: Source | None, embedder: LocalEmbedder | None = EMBEDDER):
    return StoreAnswers(
        gateway=GATEWAY,
        embedder=embedder,
        find_source=lambda database: source,
        open_executor=opened.executor,
        open_vdb=opened.vdb,
    )


def test_a_question_that_fails_still_closes_its_vdb() -> None:
    opened = Opened()
    store = answers(opened, source=SqliteSource(Path("chinook.db")))
    with pytest.raises(RuntimeError, match="the store is down"):
        asyncio.run(store.ask("q", "chinook"))
    assert [v.closed for v in opened.vdbs] == [1]
    assert opened.executors == []


def test_nothing_is_opened_without_a_source_or_an_embedder() -> None:
    opened = Opened()
    with pytest.raises(NoSource):
        asyncio.run(answers(opened, source=None).ask("q", "nope"))
    with pytest.raises(NoEmbedder):
        asyncio.run(answers(opened, source=None, embedder=None).ask("q", "chinook"))
    assert opened.vdbs == [] and opened.executors == []


class SmallModel:
    spec = BGE_SMALL


def test_a_check_is_scored_at_the_engines_models_distance(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def check_answerable(question: str, **kw: Any) -> str:
        seen.update(kw, question=question)
        return "checked"

    monkeypatch.setattr(service, "check_answerable", check_answerable)
    opened = Opened()
    store = answers(opened, source=None, embedder=cast(LocalEmbedder, SmallModel()))
    assert asyncio.run(store.check("q", "chinook")) == "checked"
    assert seen["max_distance"] == BGE_SMALL.coverage_distance == 0.345
    assert (seen["question"], seen["database"]) == ("q", "chinook")
    assert [v.closed for v in opened.vdbs] == [1]


def test_a_check_that_fails_still_closes_its_vdb() -> None:
    opened = Opened()
    store = StoreAnswers(
        gateway=cast(ClaudeGateway, ScriptedGateway(lambda task, user: None)),
        embedder=cast(LocalEmbedder, SmallModel()),
        find_source=lambda database: None,
        open_executor=opened.executor,
        open_vdb=opened.vdb,
    )
    with pytest.raises(RuntimeError, match="the store is down"):
        asyncio.run(store.check("q"))
    assert [v.closed for v in opened.vdbs] == [1]


def test_a_check_needs_an_embedder_and_no_source() -> None:
    opened = Opened()
    with pytest.raises(NoEmbedder):
        asyncio.run(answers(opened, source=None, embedder=None).check("q"))
    assert opened.vdbs == []
