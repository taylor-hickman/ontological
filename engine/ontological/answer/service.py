"""One question through the engine (BUILD_PLAN.md M1.8, M4.3, M4.4, M4.5): what `POST /ask`, its
stream and `POST /answerable` run.

`StoreAnswers.ask()` builds the grounding pack from the store's semantic layer (`build_pack`)
with hybrid search, and runs `ontological.answer.agent.ask()` to the end with the store's
ontology and the engine's one Claude gateway. With a database named, that is the scope, and its
source must exist. Without one, the pack's coverage chooses the database, and the agent's `plan`
may name another the coverage ranked (ARCHITECTURE.md §8.1).

A source executor is opened for each database the session runs SQL on, when it first needs one,
and all are closed when the question is answered or fails. The semantic VDB and its hybrid
search are the engine's, opened by the first question and kept until `close()`: the search keeps
a BM25 index of the semantic layer current, which a question shouldn't wait to rebuild.

It returns an `Asked`: the answer, the verified flag, the SQL the answer carries as
`validate_sql` passed it, the rows `run_sql` returned for it, and every session's tool log.

`StoreAnswers.stream()` (M4.5) is the same question as events, as they happen (§8.4): a `step`
for each tool call, each text the model writes between them and the restart, a `sql` once
`validate_sql` passes, a `usage` whenever Claude's rate-limit status changes, and last a
`result`, which is the `Asked`. `ask()` is the stream read to its end. Stopping the stream, by
cancelling the task that reads it, stops the session and closes the question's source
executors, which cancels a source query in flight. `ontological.answer.runs` keeps streams by
conversation for the UI to re-attach to and cancel.

`StoreAnswers.check()` is `check_answerable` (M4.4) on the same semantic layer, scored at the
engine's embedding model's coverage distance: one Haiku call, and no source or executor.

For the MCP shim (M6.2): `search_terms()` searches the Terms with the same hybrid search, and
`validate_sql()` and `run_sql()` are the gate for a caller with no agent session
(`ontological.answer.sql_checks`). They use no Claude quota. Their source executors are opened
once per database and kept until `close()`, as a calling session validates and runs one query
after another.

The API holds an `Answers`, so the API and CLI tests can answer from a fake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from auto_ontology.semantic.constants import LABEL_TERM
from auto_ontology.vdb.postgres import PostgresVDB
from ontological.answer.agent import Answered, AnswerSession, Restarted, ask
from ontological.answer.answerable import Answerability, check_answerable
from ontological.answer.models import AnswerModel
from ontological.answer.ontology import StoreCatalog, StoreOntology
from ontological.answer.pack import build_pack
from ontological.answer.retrieval import search_by_label
from ontological.answer.sql_checks import (
    NoSuchValidation,
    RanSql,
    RunFailed,
    SqlChecks,
    Validated,
)
from ontological.answer.tools import SqlRunner
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.hybrid import HybridSearch, searcher
from ontological.embed.search import semantic_vdb
from ontological.jobs.service import NoSource, source_of
from ontological.llm.calls import Outcome
from ontological.llm.gateway import ClaudeGateway, RateLimit, Said, ToolCall
from ontological.sources.executor import SourceExecutor
from ontological.sources.spec import Source

__all__ = [
    "AnswerEvent",
    "Answerability",
    "Answers",
    "Asked",
    "Finished",
    "NoEmbedder",
    "NoSource",
    "NoSuchValidation",
    "NothingFound",
    "Ran",
    "RanSql",
    "RestartStep",
    "RunFailed",
    "SaidStep",
    "SqlPassed",
    "Step",
    "StoreAnswers",
    "TermHit",
    "ToolStep",
    "Usage",
    "Validated",
    "asked",
    "events",
]

log = logging.getLogger(__name__)

SEARCH_TERMS_K = 10  # Terms search_terms returns unless asked for fewer or more

# How long the first question waits for the BM25 index's first build. Past it, the question is
# searched without BM25 until the build lands (S8: about 1.7 s at 10^5 rows).
BM25_WAIT_S = 10.0


class NoEmbedder(Exception):
    """The engine runs without the embedding model, so it can't search the ontology."""


class NothingFound(Exception):
    """A question with no database named matched nothing in any database's ontology."""


class Step(BaseModel):
    """One tool call, as the model made it and as its handler answered: `text` is what the
    model read, `data` the handler's record."""

    name: str
    args: dict[str, Any]
    is_error: bool
    text: str
    data: dict[str, Any]
    session: int = 1  # 2 for the restart's steps


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
    steps: list[Step]  # every session's tool log, in order
    outcome: Outcome | None  # the answering session's: ok, invalid, limited or error
    error: str | None
    answered_model: str | None
    restarted: str | None = None  # why a fresh session answered again, if one did


class TermHit(BaseModel):
    """One Term search found: the text its vector embeds (name, description and synonyms), and
    its cosine distance to the query, lower nearer. Hits come best first by the fused ranking."""

    id: str
    name: str
    database: str | None
    text: str
    distance: float


# --- the stream's events (§8.4) ------------------------------------------------------------------
# Each is one SSE frame, `type` first, as upstream's chat stream. The steps share `type` "step" and
# differ in `kind`. A `chart` event comes with M4.6's ResultChart, when the answer carries one.


class ToolStep(Step):
    """A tool call, once its handler has answered."""

    type: Literal["step"] = "step"
    kind: Literal["tool"] = "tool"


class SaidStep(BaseModel):
    """Text the model wrote between tool calls."""

    type: Literal["step"] = "step"
    kind: Literal["said"] = "said"
    session: int
    text: str


class RestartStep(BaseModel):
    """The first session ended without validated SQL, and session 2 starts (§8.1)."""

    type: Literal["step"] = "step"
    kind: Literal["restart"] = "restart"
    session: int
    reason: str


class SqlPassed(BaseModel):
    """SQL that passed `validate_sql`, before it runs."""

    type: Literal["sql"] = "sql"
    session: int
    sql: str
    validation_id: str


class Usage(BaseModel):
    """Claude's latest rate-limit status, from the session's RateLimitEvents (§6.5)."""

    type: Literal["usage"] = "usage"
    status: str  # allowed | allowed_warning | rejected
    window: str | None  # five_hour, seven_day, ...
    resets_at: datetime | None
    utilization: dict[str, float]

    @classmethod
    def of(cls, limit: RateLimit) -> Usage:
        return cls(
            status=limit.status,
            window=limit.window,
            resets_at=limit.resets_at,
            utilization=dict(limit.utilization),
        )


class Finished(Asked):
    """The question's end: what `POST /ask` returns."""

    type: Literal["result"] = "result"


AnswerEvent = ToolStep | SaidStep | RestartStep | SqlPassed | Usage | Finished


async def events(
    session: AnswerSession, rate_limit: Callable[[], RateLimit | None]
) -> AsyncIterator[AnswerEvent]:
    """`session`'s events as the stream's, ending with the `Finished` answer. `rate_limit` reads
    the gateway's latest status, which is sent whenever it changes."""
    sent = rate_limit()
    if sent is not None:
        yield Usage.of(sent)
    n = 1
    async for event in session:
        match event:
            case ToolCall():
                yield ToolStep(
                    name=event.name,
                    args=event.args,
                    is_error=event.is_error,
                    text=event.text,
                    data=event.data,
                    session=n,
                )
                if event.name == "validate_sql" and not event.is_error and event.data.get("ok"):
                    yield SqlPassed(
                        session=n,
                        sql=str(event.data["sql"]),
                        validation_id=str(event.data["validation_id"]),
                    )
            case Said():
                yield SaidStep(session=n, text=event.text)
            case Restarted():
                n += 1
                yield RestartStep(session=n, reason=event.reason)
        limit = rate_limit()
        if limit is not None and limit != sent:
            sent = limit
            yield Usage.of(limit)
    yield Finished.model_validate(asked(await session.answered()).model_dump())


class Answers(Protocol):
    def stream(
        self, question: str, database: str | None, *, critic: bool = True
    ) -> AsyncIterator[AnswerEvent]: ...

    async def ask(self, question: str, database: str | None, *, critic: bool = True) -> Asked: ...

    async def check(self, question: str, database: str | None = None) -> Answerability: ...

    async def search_terms(
        self, query: str, database: str | None = None, limit: int = SEARCH_TERMS_K
    ) -> list[TermHit]: ...

    async def validate_sql(
        self, sql: str, database: str, *, soft_as_warnings: bool = False
    ) -> Validated: ...

    async def run_sql(self, validation_id: str) -> RanSql: ...


async def answer_of(stream: AsyncIterator[AnswerEvent]) -> Asked:
    """The `Finished` answer at the end of `stream`, as an `Asked`."""
    async for event in stream:
        if isinstance(event, Finished):
            return Asked.model_validate(event.model_dump(exclude={"type"}))
    raise RuntimeError("the stream ended without an answer")


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
            Step(
                name=c.name,
                args=c.args,
                is_error=c.is_error,
                text=c.text,
                data=c.data,
                session=n,
            )
            for n, session in enumerate(answered.sessions or (log,), 1)
            for c in session
        ],
        outcome=answered.outcome,
        error=answered.error,
        answered_model=answered.answered_model,
        restarted=answered.restarted,
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


class SourceRunners:
    """A question's source executors, one per database, opened when the session first needs
    one (`ontological.answer.tools.Runners`)."""

    def __init__(
        self,
        find_source: Callable[[str], Source | None],
        open_executor: Callable[[Source], SourceExecutor],
    ) -> None:
        self._find = find_source
        self._open = open_executor
        self._open_ones: dict[str, SourceExecutor] = {}

    def __call__(self, database: str) -> SqlRunner:
        if database not in self._open_ones:
            source = self._find(database)
            if source is None:
                raise NoSource(database)
            self._open_ones[database] = self._open(source)
        return self._open_ones[database]

    def close(self) -> None:
        for executor in self._open_ones.values():
            executor.close()
        self._open_ones.clear()


class StoreAnswers:
    def __init__(
        self,
        *,
        gateway: ClaudeGateway,
        embedder: LocalEmbedder | None,
        find_source: Callable[[str], Source | None] = source_of,
        open_executor: Callable[[Source], SourceExecutor] = SourceExecutor,
        open_vdb: Callable[[LocalEmbedder], PostgresVDB] = semantic_vdb,
        open_hybrid: Callable[[PostgresVDB], HybridSearch] = HybridSearch,
    ) -> None:
        self._gateway = gateway
        self._embedder = embedder
        self._find_source = find_source
        self._open = open_executor
        self._open_vdb = open_vdb
        self._open_hybrid = open_hybrid
        self._searching: tuple[PostgresVDB, HybridSearch] | None = None
        self._lock = asyncio.Lock()
        self._gate_runners = SourceRunners(find_source, open_executor)
        self._gate = SqlChecks(StoreCatalog(), self._gate_runners)

    async def ask(self, question: str, database: str | None, *, critic: bool = True) -> Asked:
        """Answer `question` from `database`, or with none, from the database whose ontology
        covers it best. Uses Claude quota: one agent session, a second if the first ends without
        validated SQL, and a critic call per review."""
        return await answer_of(self.stream(question, database, critic=critic))

    async def stream(
        self, question: str, database: str | None, *, critic: bool = True
    ) -> AsyncIterator[AnswerEvent]:
        """`ask()`, as its events. Raises `NoEmbedder`, `NoSource` or `NothingFound` before the
        first one."""
        if self._embedder is None:
            raise NoEmbedder
        if database is not None:
            source = await asyncio.to_thread(self._find_source, database)
            if source is None:
                raise NoSource(database)
        vdb, hybrid = await self._search(self._embedder)
        search = searcher(hybrid)
        pack = await asyncio.to_thread(build_pack, vdb, question, database=database, search=search)
        if pack.database is None:
            raise NothingFound(question)
        runners = SourceRunners(self._find_source, self._open)
        try:
            session = ask(
                question,
                pack,
                gateway=self._gateway,
                ontology=StoreOntology(vdb, search),
                runners=runners,
                critic=critic,
            )
            async for event in events(session, lambda: self._gateway.status.rate_limit):
                yield event
        finally:
            # Cancels a source query in flight, killing its executor if it won't stop (§5.6)
            await asyncio.to_thread(runners.close)

    async def _search(self, embedder: LocalEmbedder) -> tuple[PostgresVDB, HybridSearch]:
        """The semantic VDB and its hybrid search, opened by the first question. A failure
        closes what it opened, so the next question tries again."""
        async with self._lock:
            if self._searching is None:
                vdb = await asyncio.to_thread(self._open_vdb, embedder)
                try:
                    hybrid = await asyncio.to_thread(self._open_hybrid, vdb)
                except BaseException:
                    await asyncio.to_thread(vdb.close)
                    raise
                if not await asyncio.to_thread(hybrid.bm25.wait, BM25_WAIT_S):
                    log.warning("answers: BM25 not built in %gs; searching without it", BM25_WAIT_S)
                self._searching = (vdb, hybrid)
            return self._searching

    def close(self) -> None:
        """Stop the hybrid search, close the VDB and the gate's executors, at the engine's
        shutdown."""
        self._gate_runners.close()
        if self._searching is not None:
            vdb, hybrid = self._searching
            self._searching = None
            hybrid.close()
            vdb.close()

    async def check(self, question: str, database: str | None = None) -> Answerability:
        """`check_answerable` within `database`, or in the database that covers the question
        best, at the embedder's coverage distance. Uses one `answerable_extract` call."""
        if self._embedder is None:
            raise NoEmbedder
        vdb = await asyncio.to_thread(self._open_vdb, self._embedder)
        try:
            return await check_answerable(
                question,
                gateway=self._gateway,
                vdb=vdb,
                database=database,
                max_distance=self._embedder.spec.coverage_distance,
            )
        finally:
            await asyncio.to_thread(vdb.close)

    async def search_terms(
        self, query: str, database: str | None = None, limit: int = SEARCH_TERMS_K
    ) -> list[TermHit]:
        """The Terms nearest `query`, in `database` or in every database, by hybrid search."""
        if self._embedder is None:
            raise NoEmbedder
        vdb, hybrid = await self._search(self._embedder)
        hits = await asyncio.to_thread(
            search_by_label, vdb, query, LABEL_TERM, limit, database, searcher(hybrid)
        )
        return [
            TermHit(
                id=str(h["id"]),
                name=str(h.get("name") or ""),
                database=h.get("database_name"),
                text=h["text"],
                distance=h["score"],
            )
            for h in hits
        ]

    async def validate_sql(
        self, sql: str, database: str, *, soft_as_warnings: bool = False
    ) -> Validated:
        """The gate on `sql` for `database`. Raises NoSource when the database has none."""
        return await asyncio.to_thread(
            self._gate.validate, database, sql, soft_as_warnings=soft_as_warnings
        )

    async def run_sql(self, validation_id: str) -> RanSql:
        """Run SQL that passed `validate_sql`. Raises NoSuchValidation for an id it didn't
        issue, RunFailed when the source refuses the SQL."""
        return await asyncio.to_thread(self._gate.run, validation_id)
