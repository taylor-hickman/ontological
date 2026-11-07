"""`StoreAnswers` (BUILD_PLAN.md M1.8, M4.3) keeps one semantic VDB and its hybrid search for the
engine, opened by the first question and closed at shutdown, and a question's source executors,
opened as its session needs them and closed when it is answered or fails.
tests/store/test_answer.py answers one on the real store."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from fakes import ScriptedGateway

from auto_ontology.vdb.postgres import PostgresVDB
from ontological.answer import service
from ontological.answer.pack import GroundingPack
from ontological.answer.service import (
    NoEmbedder,
    NoSource,
    NothingFound,
    SourceRunners,
    StoreAnswers,
)
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.hybrid import HybridSearch
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


class FakeBm25:
    def wait(self, timeout: float | None = None) -> bool:
        return True


class FakeHybrid:
    def __init__(self, vdb: PostgresVDB) -> None:
        self.vdb = vdb
        self.bm25 = FakeBm25()
        self.closed = 0

    def close(self, timeout: float | None = None) -> None:
        self.closed += 1


class Opened:
    def __init__(self) -> None:
        self.vdbs: list[DownVdb] = []
        self.hybrids: list[FakeHybrid] = []
        self.executors: list[Source] = []

    def vdb(self, embedder: LocalEmbedder) -> PostgresVDB:
        self.vdbs.append(DownVdb())
        return cast(PostgresVDB, self.vdbs[-1])

    def hybrid(self, vdb: PostgresVDB) -> HybridSearch:
        self.hybrids.append(FakeHybrid(vdb))
        return cast(HybridSearch, self.hybrids[-1])

    def executor(self, source: Source) -> SourceExecutor:
        self.executors.append(source)
        raise AssertionError("no executor before the pack is built")


def answers(
    opened: Opened,
    *,
    source: Source | None,
    embedder: LocalEmbedder | None = EMBEDDER,
    hybrid: bool = False,
) -> StoreAnswers:
    return StoreAnswers(
        gateway=GATEWAY,
        embedder=embedder,
        find_source=lambda database: source,
        open_executor=opened.executor,
        open_vdb=opened.vdb,
        **({"open_hybrid": opened.hybrid} if hybrid else {}),
    )


def test_a_search_that_fails_to_open_closes_its_vdb_and_the_next_question_tries_again() -> None:
    opened = Opened()
    store = answers(opened, source=SqliteSource(Path("chinook.db")))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="the store is down"):
            asyncio.run(store.ask("q", "chinook"))
    assert [v.closed for v in opened.vdbs] == [1, 1]
    assert opened.executors == []


def unmatched(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """build_pack finds nothing, in whichever database it is asked about."""
    scopes: list[str | None] = []

    def build_pack(vdb: Any, question: str, *, database: str | None, search: Any) -> GroundingPack:
        scopes.append(database)
        return GroundingPack(question, None, [], [], {}, [], None)

    monkeypatch.setattr(service, "build_pack", build_pack)
    return scopes


def test_the_search_is_opened_once_kept_across_questions_and_closed_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = unmatched(monkeypatch)
    opened = Opened()
    store = answers(opened, source=None, hybrid=True)

    async def twice() -> None:
        for _ in range(2):
            with pytest.raises(NothingFound):
                await store.ask("which plants need sun", None)

    asyncio.run(twice())
    assert scopes == [None, None]  # no database named: no source looked up first
    assert len(opened.vdbs) == 1 and len(opened.hybrids) == 1
    assert opened.vdbs[0].closed == 0
    store.close()
    assert opened.vdbs[0].closed == 1 and opened.hybrids[0].closed == 1
    store.close()  # once is enough
    assert opened.vdbs[0].closed == 1


class FakeExecutor:
    def __init__(self, source: Source) -> None:
        self.source = source
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def test_a_questions_runners_open_each_database_once_and_close_them_all() -> None:
    sources = {"chinook": SqliteSource(Path("chinook.db")), "garden": SqliteSource(Path("g.db"))}
    opened: list[FakeExecutor] = []

    def open_executor(source: Source) -> SourceExecutor:
        opened.append(FakeExecutor(source))
        return cast(SourceExecutor, opened[-1])

    runners = SourceRunners(sources.get, open_executor)
    assert runners("chinook") is runners("chinook")
    runners("garden")
    with pytest.raises(NoSource):
        runners("nope")
    assert [e.source for e in opened] == [sources["chinook"], sources["garden"]]
    runners.close()
    assert [e.closed for e in opened] == [1, 1]


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
