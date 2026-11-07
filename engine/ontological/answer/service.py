"""One question through the engine (BUILD_PLAN.md M1.8): what `POST /ask` runs.

`StoreAnswers.ask()` finds the source the database was ingested from, builds the grounding pack
from the store's semantic layer (`build_pack`), opens a source executor for the agent's
`run_sql`, and runs `ontological.answer.agent.ask()` to the end with the store's ontology and the
engine's one Claude gateway. The semantic VDB and the executor are the question's own, and are
closed when it is answered or fails: a VDB holds a connection pool and a loop thread until then.

It returns an `Asked`: the answer, the verified flag, the SQL the answer carries as
`validate_sql` passed it, the rows `run_sql` returned for it, and the tool log.

The API holds an `Answers`, so the API and CLI tests can answer from a fake.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from pydantic import BaseModel

from auto_ontology.vdb.postgres import PostgresVDB
from ontological.answer.agent import Answered, ask
from ontological.answer.models import AnswerModel
from ontological.answer.ontology import StoreOntology
from ontological.answer.pack import build_pack
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import semantic_vdb
from ontological.jobs.service import NoSource, source_of
from ontological.llm.calls import Outcome
from ontological.llm.gateway import ClaudeGateway, ToolCall
from ontological.sources.executor import SourceExecutor
from ontological.sources.spec import Source

__all__ = ["Answers", "Asked", "NoEmbedder", "NoSource", "Ran", "Step", "StoreAnswers", "asked"]


class NoEmbedder(Exception):
    """The engine runs without the embedding model, so it can't search the ontology."""


class Step(BaseModel):
    """One tool call, as the model made it and as its handler answered: `text` is what the
    model read, `data` the handler's record."""

    name: str
    args: dict[str, Any]
    is_error: bool
    text: str
    data: dict[str, Any]


class Ran(BaseModel):
    """What `run_sql` returned: at most `MAX_ANSWER_ROWS` rows, `truncated` if there were more."""

    columns: list[str]
    types: list[str]
    rows: list[list[Any]]
    truncated: bool


class Asked(BaseModel):
    question: str
    database: str
    answer: AnswerModel | None  # None when the session ended without one
    verified: bool
    reason: str | None  # why the answer is unverified
    sql: str | None  # the answer's SQL as validate_sql passed it; None if none passed
    result: Ran | None  # what run_sql returned for that SQL; None if it didn't run
    steps: list[Step]  # the tool log, in order
    outcome: Outcome | None  # the session's: ok, invalid, limited or error
    error: str | None
    answered_model: str | None


class Answers(Protocol):
    async def ask(self, question: str, database: str, *, critic: bool = True) -> Asked: ...


def asked(answered: Answered) -> Asked:
    log = answered.tool_log
    validation_id = answered.answer.validation_id if answered.answer else None
    validated = _last(log, "validate_sql", validation_id)
    ran = _last(log, "run_sql", validation_id)
    return Asked(
        question=answered.question,
        database=answered.database,
        answer=answered.answer,
        verified=answered.verified,
        reason=answered.reason,
        sql=str(validated.data["sql"]) if validated and validated.data.get("ok") else None,
        result=Ran.model_validate(ran.data) if ran else None,
        steps=[
            Step(name=c.name, args=c.args, is_error=c.is_error, text=c.text, data=c.data)
            for c in log
        ],
        outcome=answered.outcome,
        error=answered.error,
        answered_model=answered.answered_model,
    )


def _last(log: Sequence[ToolCall], name: str, validation_id: str | None) -> ToolCall | None:
    """The latest successful `name` call for `validation_id`."""
    if validation_id is None:
        return None
    found = [
        c
        for c in log
        if c.name == name and not c.is_error and c.data.get("validation_id") == validation_id
    ]
    return found[-1] if found else None


class StoreAnswers:
    def __init__(
        self,
        *,
        gateway: ClaudeGateway,
        embedder: LocalEmbedder | None,
        find_source: Callable[[str], Source | None] = source_of,
        open_executor: Callable[[Source], SourceExecutor] = SourceExecutor,
        open_vdb: Callable[[LocalEmbedder], PostgresVDB] = semantic_vdb,
    ) -> None:
        self._gateway = gateway
        self._embedder = embedder
        self._find_source = find_source
        self._open = open_executor
        self._open_vdb = open_vdb

    async def ask(self, question: str, database: str, *, critic: bool = True) -> Asked:
        """Answer `question` from `database`. Uses Claude quota: one agent session, and a
        critic call per review."""
        if self._embedder is None:
            raise NoEmbedder
        source = await asyncio.to_thread(self._find_source, database)
        if source is None:
            raise NoSource(database)
        vdb = await asyncio.to_thread(self._open_vdb, self._embedder)
        try:
            pack = await asyncio.to_thread(build_pack, vdb, question, database=database)
            executor = await asyncio.to_thread(self._open, source)
            try:
                session = ask(
                    question,
                    pack,
                    gateway=self._gateway,
                    ontology=StoreOntology(vdb),
                    runner=executor,
                    critic=critic,
                )
                return asked(await session.answered())
            finally:
                await asyncio.to_thread(executor.close)
        finally:
            await asyncio.to_thread(vdb.close)
