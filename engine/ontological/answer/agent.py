"""One question, answered by the gated agent (ARCHITECTURE.md §8.1).

`ask()` takes the grounding pack the pre-work built (`ontological.answer.pack.build_pack`), opens
one `answer_agent` session with the six tools and the pack in its system prompt, and returns an
`AnswerSession`. Iterate it for the steps as they happen; `answered()` waits for the end and runs
the post-run check on the tool log. The critic is upstream's intent validation, one
`answer_review` structured call per `review_answer`.

The restart when a session ends without validated SQL is M4.3's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from ontological.answer.models import AnswerModel, Review
from ontological.answer.pack import GroundingPack
from ontological.answer.postcheck import Verdict, post_run_check
from ontological.answer.prompts import (
    AGENT_PROMPT_VERSION,
    REVIEW_PROMPT_VERSION,
    agent_system_prompt,
    render_join_paths,
    review_prompt,
    review_system_prompt,
)
from ontological.answer.tools import AnswerTools, Ontology, Reviewer, ReviewInput, SqlRunner
from ontological.llm.calls import Outcome
from ontological.llm.gateway import AGENT_MAX_TURNS, AgentEvent, AgentRun, ClaudeGateway, ToolCall


@dataclass(frozen=True)
class Answered:
    question: str
    database: str
    answer: AnswerModel | None  # None when the session ended without one
    verdict: Verdict
    tool_log: list[ToolCall]
    outcome: Outcome | None  # the session's: ok, invalid, limited or error
    error: str | None
    answered_model: str | None

    @property
    def verified(self) -> bool:
        return self.verdict.verified

    @property
    def reason(self) -> str | None:
        """Why the answer is unverified, for the user."""
        return self.verdict.reason


class AnswerSession:
    def __init__(
        self, question: str, database: str, run: AgentRun[AnswerModel], *, critic: bool
    ) -> None:
        self.question = question
        self.database = database
        self.run = run
        self.critic = critic

    def __aiter__(self) -> AsyncIterator[AgentEvent]:
        return self.run.__aiter__()

    async def answered(self) -> Answered:
        run = await self.run.wait()
        return Answered(
            question=self.question,
            database=self.database,
            answer=run.output,
            verdict=post_run_check(run.tool_log, run.output, critic=self.critic),
            tool_log=list(run.tool_log),
            outcome=run.outcome,
            error=run.error,
            answered_model=run.answered_model,
        )


def ask(
    question: str,
    pack: GroundingPack,
    *,
    gateway: ClaudeGateway,
    ontology: Ontology,
    runner: SqlRunner,
    critic: bool = True,
    max_turns: int = AGENT_MAX_TURNS,
) -> AnswerSession:
    """Start answering `question` from `pack.database`, which is the scope when one was given
    (`build_pack(..., database=scope)`) and otherwise the one the pack's coverage chose.
    `runner` runs SQL on that database's source. `critic` is the `answer.critic` setting."""
    if pack.database is None:
        raise ValueError("the grounding pack names no database to answer from")
    tools = AnswerTools(
        question,
        database=pack.database,
        ontology=ontology,
        runner=runner,
        review=make_critic(gateway, pack) if critic else None,
    )
    run = gateway.agent(
        "answer_agent",
        agent_system_prompt(pack, critic=critic),
        question,
        tools.sdk_tools(),
        AnswerModel,
        max_turns,
        prompt_version=AGENT_PROMPT_VERSION,
    )
    return AnswerSession(question, pack.database, run, critic=critic)


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
