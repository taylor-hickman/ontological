"""One question, end to end (ARCHITECTURE.md §8.1, BUILD_PLAN.md M1.7): the grounding pack in the
system prompt, the agent session on `answer_agent` with the six tools, the critic's
`answer_review` call from inside it, and the post-run check on the tool log. The model is a
scripted `query()` that calls the tools through the SDK's MCP bridge."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

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
from ontological.answer.agent import Answered, ask
from ontological.answer.prompts import REFUSAL_RULE
from test_tools import GOOD_SQL, PLAN, FakeOntology, FakeRunner

from ontological.answer.pack import GroundingPack
from ontological.llm.gateway import ClaudeGateway

QUESTION = "How many albums does each artist have?"
PACK = GroundingPack(
    question=QUESTION,
    database="chinook",
    attributes=[
        {
            "id": "a-1",
            "attr_name": "Artist Name",
            "attr_description": "The artist's name.",
            "col_name": "Name",
            "table_name": "Artist",
            "schema_name": "main",
            "database_name": "chinook",
            "term_name": "Artist",
            "datatype": "TEXT",
            "score": 0.2,
        }
    ],
    terms=[{"id": "t-1", "name": "Artist", "text": "Term: Artist. A musician.", "score": 0.3}],
    term_synonyms={},
    tables=[
        {
            "id": "t-artist",
            "name": "Artist",
            "schema_name": "main",
            "columns": [{"name": "ArtistId"}, {"name": "Name"}],
        }
    ],
    anchor=None,
)

ANSWER = {
    "answer": "AC/DC and Accept have two albums each.",
    "sql": GOOD_SQL,
    "plan_id": "plan-1",
    "validation_id": "val-1",
    "review_id": "rev-1",
    "answerable": True,
}
REVIEW_PASSES = {"reasoning": "Counts albums per artist.", "is_valid": True}


def agent(*calls: Any, output: Any = ANSWER) -> list[Any]:
    return [init(OPUS), limit(), *calls, said("Done.", model=OPUS), result(structured=output)]


FULL = [
    Call("plan", PLAN),
    Call("validate_sql", {"sql": GOOD_SQL}),
    Call("run_sql", {"validation_id": "val-1"}),
    Call("review_answer", {"validation_id": "val-1", "answer_draft": ANSWER["answer"]}),
]


class Harness:
    def __init__(self, tmp_path: Path, scripts: list[list[Any]]) -> None:
        self.query = FakeQuery(scripts)
        self.calls = MemoryCalls()
        self.gateway = ClaudeGateway(
            calls=self.calls,
            sandbox=tmp_path / "claude-sandbox",
            cli_path=fake_claude(tmp_path),
            query=self.query,
        )
        self.runner = FakeRunner()

    def ask(self, *, critic: bool = True) -> Answered:
        async def go() -> Answered:
            session = ask(
                QUESTION,
                PACK,
                gateway=self.gateway,
                ontology=FakeOntology(),
                runner=self.runner,
                critic=critic,
            )
            return await session.answered()

        return asyncio.run(go())


def test_the_full_sequence_gives_a_verified_answer(tmp_path: Path) -> None:
    review = structured_session(REVIEW_PASSES, model=SONNET)
    h = Harness(tmp_path, [agent(*FULL), review])
    got = h.ask()

    assert got.verified and got.reason is None
    assert got.answer is not None and got.answer.sql == GOOD_SQL
    assert got.answered_model == OPUS
    assert [c.name for c in got.tool_log] == [
        "plan",
        "validate_sql",
        "run_sql",
        "review_answer",
    ]
    assert h.runner.ran == [GOOD_SQL]
    assert [(r.task, r.kind) for r in h.calls.rows] == [
        ("answer_review", "structured"),
        ("answer_agent", "agent"),
    ]


def test_the_session_is_on_answer_agent_with_the_pack_and_the_refusal_rule(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, [agent(*FULL), structured_session(REVIEW_PASSES, model=SONNET)])
    h.ask()
    spawn, review = h.query.spawns
    o = spawn.options
    assert spawn.prompt == QUESTION
    assert (o.model, o.effort) == ("opus", "medium")
    assert o.max_turns == 16
    system = str(o.system_prompt)
    assert REFUSAL_RULE in system
    assert "Artist Name" in system and "main.Artist" in system and "DuckDB" in system
    assert [t.removeprefix("mcp__ontological__") for t in o.allowed_tools or []] == [
        "plan",
        "search_ontology",
        "describe_table",
        "validate_sql",
        "run_sql",
        "review_answer",
    ]
    # The critic is upstream's intent validation, on answer_review, shown the rows
    assert (review.options.model, review.options.effort) == ("sonnet", "medium")
    assert QUESTION in review.prompt and GOOD_SQL in review.prompt and "AC/DC" in review.prompt


def test_an_answer_that_skipped_the_review_is_unverified(tmp_path: Path) -> None:
    h = Harness(tmp_path, [agent(*FULL[:3], output={**ANSWER, "review_id": None})])
    got = h.ask()
    assert got.answer is not None
    assert not got.verified and got.reason is not None and "review" in got.reason


def test_with_the_critic_off_the_session_has_no_review_and_needs_none(tmp_path: Path) -> None:
    h = Harness(tmp_path, [agent(*FULL[:3], output={**ANSWER, "review_id": None})])
    got = h.ask(critic=False)
    assert got.verified
    allowed = h.query.spawns[0].options.allowed_tools or []
    assert "mcp__ontological__review_answer" not in allowed


def test_a_session_without_an_answer_is_unverified(tmp_path: Path) -> None:
    out_of_turns = result(is_error=True, subtype="error_max_turns")
    h = Harness(tmp_path, [[init(OPUS), *FULL[:2], out_of_turns]])
    got = h.ask()
    assert got.answer is None and not got.verified
    assert got.reason is not None and "no answer" in got.reason
    assert got.outcome == "invalid" and len(got.tool_log) == 2
