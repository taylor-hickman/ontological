"""One question, end to end (ARCHITECTURE.md §8.1, BUILD_PLAN.md M1.7, M4.3): the grounding pack in
the system prompt, the agent session on `answer_agent` with its tools, the critic's
`answer_review` call from inside it, the post-run check on the tool log, and the one restart
when a session ends without validated SQL. The model is a scripted `query()` that calls the tools
through the SDK's MCP bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
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
from test_tools import GOOD_SQL, PLAN, FakeOntology, FakeRunner

from ontological.answer import service
from ontological.answer.agent import Answered, Restarted, SessionEvent, ask
from ontological.answer.pack import GroundingPack
from ontological.answer.prompts import REFUSAL_RULE
from ontological.answer.retrieval import Coverage
from ontological.answer.service import asked
from ontological.llm.gateway import ClaudeGateway, RateLimit, ToolCall

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
    def __init__(self, tmp_path: Path, scripts: list[Sequence[Any]]) -> None:
        self.query = FakeQuery(scripts)
        self.calls = MemoryCalls()
        self.gateway = ClaudeGateway(
            calls=self.calls,
            sandbox=tmp_path / "claude-sandbox",
            cli_path=fake_claude(tmp_path),
            query=self.query,
        )
        self.runner = FakeRunner()

    def ask(self, *, critic: bool = True, pack: GroundingPack = PACK) -> Answered:
        async def go() -> Answered:
            session = ask(
                QUESTION,
                pack,
                gateway=self.gateway,
                ontology=FakeOntology(),
                runners=lambda database: self.runner,
                critic=critic,
            )
            return await session.answered()

        return asyncio.run(go())

    def events(self) -> list[SessionEvent]:
        async def go() -> list[SessionEvent]:
            session = ask(
                QUESTION,
                PACK,
                gateway=self.gateway,
                ontology=FakeOntology(),
                runners=lambda database: self.runner,
            )
            return [event async for event in session]

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
        "get_term",
        "describe_table",
        "join_path",
        "column_values",
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


OUT_OF_TURNS = result(is_error=True, subtype="error_max_turns")


def test_a_session_without_an_answer_is_unverified(tmp_path: Path) -> None:
    h = Harness(tmp_path, [[init(OPUS), *FULL[:2], OUT_OF_TURNS]])
    got = h.ask()
    assert got.answer is None and not got.verified
    assert got.reason is not None and "no answer" in got.reason
    assert got.outcome == "invalid" and len(got.tool_log) == 2


# --- the one restart (M4.3) ---------------------------------------------------------------------


def test_a_session_without_validated_sql_restarts_once_and_the_restart_answers(
    tmp_path: Path,
) -> None:
    failed = Call("validate_sql", {"sql": "SELECT Titel FROM Album"})
    first = agent(Call("plan", PLAN), failed, output={**ANSWER, "validation_id": None})
    h = Harness(tmp_path, [first, agent(*FULL), structured_session(REVIEW_PASSES, model=SONNET)])
    got = h.ask()

    assert got.verified and got.restarted is not None and "validate_sql" in got.restarted
    assert [len(s) for s in got.sessions] == [2, 4] and got.answered_session == 1
    assert [c.name for c in got.tool_log] == ["plan", "validate_sql", "run_sql", "review_answer"]
    agents = [r for r in h.calls.rows if r.task == "answer_agent"]
    assert len(agents) == 2
    # Each session has tools of its own: the restart's ids start over
    assert got.answer is not None and got.answer.plan_id == "plan-1"

    steps = asked(got).steps
    assert [s.session for s in steps] == [1, 1, 2, 2, 2, 2]
    assert asked(got).sql == GOOD_SQL and asked(got).restarted == got.restarted


def test_a_session_that_ran_out_of_turns_restarts(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        [
            [init(OPUS), Call("plan", PLAN), OUT_OF_TURNS],
            agent(*FULL),
            structured_session(REVIEW_PASSES, model=SONNET),
        ],
    )
    got = h.ask()
    assert got.verified and got.restarted is not None and "no answer" in got.restarted


def test_there_is_only_one_restart(tmp_path: Path) -> None:
    h = Harness(tmp_path, [[init(OPUS), Call("plan", PLAN), OUT_OF_TURNS]] * 3)
    got = h.ask()
    assert len(got.sessions) == 2 and len(h.query.spawns) == 2
    assert got.answer is None and not got.verified


def test_when_the_restart_has_no_answer_the_first_sessions_stands(tmp_path: Path) -> None:
    unanswerable = {**ANSWER, "sql": None, "validation_id": None, "review_id": None}
    unanswerable |= {"answerable": False, "missing": ["a table of tours"]}
    failed = Call("validate_sql", {"sql": "SELECT Titel FROM Album"})
    h = Harness(
        tmp_path,
        [agent(Call("plan", PLAN), failed, output=unanswerable), [init(OPUS), OUT_OF_TURNS]],
    )
    got = h.ask()
    assert got.answered_session == 0 and got.answer is not None
    assert got.answer.missing == ["a table of tours"]
    assert [s.session for s in asked(got).steps] == [1, 1]


@pytest.mark.parametrize(
    ("plan", "output"),
    [
        ({**PLAN, "intent": "information"}, {"sql": None, "validation_id": None}),
        (
            {"intent": "prediction", "entities": ["albums next year"]},
            {"sql": None, "validation_id": None, "answerable": False},
        ),
        (PLAN, {"sql": None, "validation_id": None, "answerable": False}),  # judged, not tried
    ],
)
def test_answers_that_need_no_sql_do_not_restart(
    tmp_path: Path, plan: dict[str, Any], output: dict[str, Any]
) -> None:
    final = {**ANSWER, "review_id": None, "missing": ["prediction_unsupported"], **output}
    h = Harness(tmp_path, [agent(Call("plan", plan), output=final)])
    got = h.ask()
    assert got.restarted is None and len(h.query.spawns) == 1
    assert got.verified, got.reason


def test_a_session_that_hit_the_usage_limit_does_not_restart(tmp_path: Path) -> None:
    rejected = [init(OPUS), limit("rejected"), result(is_error=True, api_error_status=429)]
    h = Harness(tmp_path, [rejected])
    got = h.ask()
    assert got.outcome == "limited" and got.restarted is None and len(h.query.spawns) == 1


def test_the_events_say_where_the_restart_begins(tmp_path: Path) -> None:
    first = [init(OPUS), Call("plan", PLAN), OUT_OF_TURNS]
    h = Harness(tmp_path, [first, agent(*FULL), structured_session(REVIEW_PASSES, model=SONNET)])
    events = h.events()
    names = [e.name if isinstance(e, ToolCall) else type(e).__name__ for e in events]
    restart = names.index("Restarted")
    assert isinstance(events[restart], Restarted)
    assert names[:restart].count("plan") == 1 and names[restart + 1 :].count("plan") == 1


# --- a question with no scope (M4.3) ---------------------------------------------------------


def test_without_a_scope_the_prompt_ranks_the_databases_and_plan_may_name_one(
    tmp_path: Path,
) -> None:
    ranked = [
        Coverage("chinook", frozenset({QUESTION}), 2, 0.4),
        Coverage("northwind", frozenset(), 1, 0.6),
    ]
    pack = replace(PACK, coverage=ranked)
    planned = {**PLAN, "database": "northwind"}
    calls = [Call("plan", planned), *FULL[1:]]
    h = Harness(tmp_path, [agent(*calls), structured_session(REVIEW_PASSES, model=SONNET)])
    got = h.ask(pack=pack)

    system = str(h.query.spawns[0].options.system_prompt)
    assert "- chinook: 1 of the question's searches covered, 2 hit(s)" in system
    assert "- northwind: 0 of" in system and "name it in plan's database" in system
    assert got.verified and got.database == "northwind"


# --- what POST /ask returns (M1.8) -----------------------------------------------------------


def test_asked_carries_the_validated_sql_its_rows_and_every_step(tmp_path: Path) -> None:
    h = Harness(tmp_path, [agent(*FULL), structured_session(REVIEW_PASSES, model=SONNET)])
    got = asked(h.ask())

    assert got.verified and got.sql == GOOD_SQL
    assert got.result is not None
    assert got.result.columns == ["Name", "albums"]
    assert got.result.rows == [["AC/DC", 2], ["Accept", 2]]
    assert [s.name for s in got.steps] == ["plan", "validate_sql", "run_sql", "review_answer"]
    assert got.steps[2].data["row_count"] == 2
    assert got.answered_model == OPUS
    assert got.model_dump(mode="json")["result"]["rows"][0] == ["AC/DC", 2]  # as the route sends


def test_asked_has_no_sql_or_rows_for_an_id_that_never_passed(tmp_path: Path) -> None:
    h = Harness(tmp_path, [agent(*FULL[:2], output={**ANSWER, "validation_id": "val-9"})])
    got = asked(h.ask())
    assert not got.verified
    assert got.sql is None and got.result is None
    assert got.answer is not None and got.answer.sql == GOOD_SQL  # what the model claimed


def test_asked_without_an_answer_keeps_the_steps(tmp_path: Path) -> None:
    # validate_sql passed, so the session doesn't restart
    got = asked(Harness(tmp_path, [[init(OPUS), *FULL[:2], OUT_OF_TURNS]]).ask())
    assert got.answer is None and got.sql is None and got.result is None
    assert [s.name for s in got.steps] == ["plan", "validate_sql"]
    assert got.outcome == "invalid"


# --- the stream's events (M4.5) --------------------------------------------------------------


def test_the_stream_sends_each_step_the_sql_once_it_passes_the_usage_and_the_answer_last(
    tmp_path: Path,
) -> None:
    first = [init(OPUS), Call("plan", PLAN), OUT_OF_TURNS]
    h = Harness(tmp_path, [first, agent(*FULL), structured_session(REVIEW_PASSES, model=SONNET)])

    async def go() -> list[service.AnswerEvent]:
        session = ask(
            QUESTION,
            PACK,
            gateway=h.gateway,
            ontology=FakeOntology(),
            runners=lambda database: h.runner,
        )
        return [e async for e in service.events(session, lambda: h.gateway.status.rate_limit)]

    got = asyncio.run(go())

    def label(e: service.AnswerEvent) -> str:
        match e:
            case service.ToolStep():
                return f"{e.session} {e.name}"
            case service.SaidStep():
                return f"{e.session} said"
            case service.RestartStep():
                return f"{e.session} restart"
            case service.SqlPassed():
                return f"{e.session} sql"
            case service.Usage():
                return "usage"
            case service.Finished():
                return "result"

    assert [label(e) for e in got] == [
        "1 plan",
        "2 restart",
        "2 plan",
        "usage",  # the session's RateLimitEvent came before its first tool call
        "2 validate_sql",
        "2 sql",
        "2 run_sql",
        "2 review_answer",
        "2 said",
        "result",
    ]
    restart = got[1]
    assert isinstance(restart, service.RestartStep) and "turns" in restart.reason
    sql = got[5]
    assert isinstance(sql, service.SqlPassed) and sql.sql == GOOD_SQL
    assert sql.validation_id == "val-1"
    usage = got[3]
    assert isinstance(usage, service.Usage) and usage.status == "allowed"
    assert usage.window == "seven_day" and usage.utilization == {"seven_day": 0.4}
    done = got[-1]
    assert isinstance(done, service.Finished) and done.type == "result"
    assert done.verified and done.sql == GOOD_SQL and done.restarted is not None
    assert [s.session for s in done.steps] == [1, 2, 2, 2, 2]


def test_a_usage_already_known_is_sent_first_and_an_unchanged_one_not_again(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, [agent(*FULL), structured_session(REVIEW_PASSES, model=SONNET)])
    h.gateway._rate_limit = RateLimit("allowed", "seven_day", None, {"seven_day": 0.4})  # pyright: ignore[reportPrivateUsage]

    async def go() -> list[service.AnswerEvent]:
        session = ask(
            QUESTION,
            PACK,
            gateway=h.gateway,
            ontology=FakeOntology(),
            runners=lambda database: h.runner,
        )
        return [e async for e in service.events(session, lambda: h.gateway.status.rate_limit)]

    got = asyncio.run(go())
    usages = [i for i, e in enumerate(got) if isinstance(e, service.Usage)]
    # The script's own event has a reset time, so it differs once, then stays the same
    assert usages == [0, 2]
