"""One question, answered by the gated agent (ARCHITECTURE.md §8.1).

`ask()` takes the grounding pack the pre-work built (`ontological.answer.pack.build_pack`), opens
an `answer_agent` session with the tools and the pack in its system prompt, and returns an
`AnswerSession`. Iterate it for the steps as they happen; `answered()` waits for the end and runs
the post-run check on the tool log. The critic is upstream's intent validation, one
`answer_review` structured call per `review_answer`.

**The restart.** When a session ends without SQL that passed `validate_sql`, one fresh session
answers the question again, with new tools and the same pack, as upstream restarts its graph
once when no SQL comes out (`restart_reason`). Answers that need no SQL don't restart: one under
an `information` or `prediction` plan, or `answerable=false` from a session that never tried
SQL. Nor does a session that hit the usage limit, which a fresh one would hit again. The answer
is the restart's, unless it ended with none and the first session had one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from ontological.answer.models import AnswerModel, Review
from ontological.answer.pack import GroundingPack
from ontological.answer.postcheck import Verdict, post_run_check
from ontological.answer.prompts import (
    AGENT_PROMPT_VERSION,
    REVIEW_PROMPT_VERSION,
    agent_system_prompt,
    review_prompt,
    review_system_prompt,
)
from ontological.answer.render import render_join_paths
from ontological.answer.tools import AnswerTools, Ontology, Reviewer, ReviewInput, Runners
from ontological.llm.calls import Outcome
from ontological.llm.gateway import AGENT_MAX_TURNS, AgentEvent, AgentRun, ClaudeGateway, ToolCall

MAX_SESSIONS = 2  # the first and the one restart


@dataclass(frozen=True)
class Restarted:
    """The first session ended without validated SQL, and a fresh one starts."""

    reason: str


SessionEvent = AgentEvent | Restarted


@dataclass(frozen=True)
class Answered:
    question: str
    database: str  # the answer's plan's, or the pack's
    answer: AnswerModel | None  # None when no session ended with one
    verdict: Verdict
    tool_log: list[ToolCall]  # the answering session's
    outcome: Outcome | None  # the answering session's: ok, invalid, limited or error
    error: str | None
    answered_model: str | None
    # Every session's tool log, in order, and which one answered: two after a restart
    sessions: tuple[list[ToolCall], ...] = ()
    answered_session: int = 0
    restarted: str | None = None  # why the first session was restarted

    @property
    def verified(self) -> bool:
        return self.verdict.verified

    @property
    def reason(self) -> str | None:
        """Why the answer is unverified, for the user."""
        return self.verdict.reason


class AnswerSession:
    def __init__(
        self,
        question: str,
        pack: GroundingPack,
        start: Callable[[], AgentRun[AnswerModel]],
        *,
        critic: bool,
    ) -> None:
        assert pack.database is not None
        self.question = question
        self.database = pack.database
        self.critic = critic
        self.runs: list[AgentRun[AnswerModel]] = []
        self._start = start
        self._events: AsyncIterator[SessionEvent] | None = None
        self.restarted: str | None = None

    def __aiter__(self) -> AsyncIterator[SessionEvent]:
        if self._events is None:
            self._events = self._sessions()
        return self._events

    async def _sessions(self) -> AsyncIterator[SessionEvent]:
        while True:
            run = self._start()
            self.runs.append(run)
            async for event in run:
                yield event
            if len(self.runs) >= MAX_SESSIONS:
                return
            reason = restart_reason(run)
            if reason is None:
                return
            self.restarted = reason
            yield Restarted(reason)

    async def answered(self) -> Answered:
        async for _ in self:
            pass
        index = len(self.runs) - 1
        if self.runs[index].output is None and index > 0 and self.runs[0].output is not None:
            index = 0
        run = self.runs[index]
        return Answered(
            question=self.question,
            database=_answer_database(run) or self.database,
            answer=run.output,
            verdict=post_run_check(run.tool_log, run.output, critic=self.critic),
            tool_log=list(run.tool_log),
            outcome=run.outcome,
            error=run.error,
            answered_model=run.answered_model,
            sessions=tuple(list(r.tool_log) for r in self.runs),
            answered_session=index,
            restarted=self.restarted,
        )


def restart_reason(run: AgentRun[AnswerModel]) -> str | None:
    """Why `run` needs a fresh session, or None when it doesn't (see the module's docstring)."""
    if run.outcome == "limited":
        return None
    log = run.tool_log
    if _passed(log, "validate_sql"):
        return None
    answer = run.output
    if answer is not None:
        intent = _plans(log).get(answer.plan_id, {}).get("intent")
        if intent in ("information", "prediction"):
            return None
        if not answer.answerable and not any(c.name == "validate_sql" for c in log):
            return None
    if answer is None:
        return f"the session ended with no answer ({run.outcome}: {run.error or 'no reason'})"
    return "the session ended without SQL that passed validate_sql"


def _passed(log: Sequence[ToolCall], tool: str) -> bool:
    return any(c.name == tool and not c.is_error for c in log)


def _plans(log: Sequence[ToolCall]) -> dict[str, dict[str, object]]:
    return {
        str(c.data["plan_id"]): c.data
        for c in log
        if c.name == "plan" and not c.is_error and c.data.get("plan_id")
    }


def _answer_database(run: AgentRun[AnswerModel]) -> str | None:
    if run.output is None:
        return None
    database = _plans(run.tool_log).get(run.output.plan_id, {}).get("database")
    return str(database) if database else None


def ask(
    question: str,
    pack: GroundingPack,
    *,
    gateway: ClaudeGateway,
    ontology: Ontology,
    runners: Runners,
    critic: bool = True,
    max_turns: int = AGENT_MAX_TURNS,
) -> AnswerSession:
    """Start answering `question` from `pack.database`, which is the scope when one was given
    (`build_pack(..., database=scope)`) and otherwise the one the pack's coverage chose; without
    a scope, `plan` may name another database the coverage ranked. `runners` opens a database's
    source to run SQL on. `critic` is the `answer.critic` setting."""
    if pack.database is None:
        raise ValueError("the grounding pack names no database to answer from")
    database = pack.database
    system = agent_system_prompt(pack, critic=critic)
    review = make_critic(gateway, pack) if critic else None

    def start() -> AgentRun[AnswerModel]:
        tools = AnswerTools(
            question,
            database=database,
            others=[c.database for c in pack.coverage],
            ontology=ontology,
            runners=runners,
            review=review,
            shown_tables=[str(t["id"]) for t in pack.tables],
        )
        return gateway.agent(
            "answer_agent",
            system,
            question,
            tools.sdk_tools(),
            AnswerModel,
            max_turns,
            prompt_version=AGENT_PROMPT_VERSION,
        )

    return AnswerSession(question, pack, start, critic=critic)


def make_critic(gateway: ClaudeGateway, pack: GroundingPack) -> Reviewer:
    """Upstream's intent validation on `answer_review` (Sonnet, §6.3), shown the pack's join
    paths as its authoritative ones."""
    join_paths = render_join_paths(pack)

    async def review(given: ReviewInput) -> Review | None:
        return await gateway.structured(
            "answer_review",
            review_system_prompt(given),
            review_prompt(given, join_paths=join_paths),
            Review,
            prompt_version=REVIEW_PROMPT_VERSION,
        )

    return review
