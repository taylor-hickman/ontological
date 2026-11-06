"""ClaudeGateway.agent() (ARCHITECTURE.md §6.1, §6.2, §8.1): one session with Ontological's SDK MCP
tools and the answer's JSON schema, against a scripted `query()` that calls the tools through the
SDK's own MCP bridge. The run records every tool call in order, for the post-run check."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool
from pydantic import BaseModel
from sdk_fakes import (
    OPUS,
    SONNET,
    Call,
    FakeQuery,
    MemoryCalls,
    answered,
    fake_claude,
    fell_back,
    init,
    limit,
    result,
    said,
)

from ontological.llm.env import call_env
from ontological.llm.gateway import AgentRun, ClaudeGateway, Said, ToolCall

SYSTEM = "You answer by calling tools. A tool may refuse and name the missing step."
USER = "How many albums are there?"


class Answer(BaseModel):
    answer: str
    plan_id: str


def tools(log: list[str], gateway: ClaudeGateway | None = None) -> list[SdkMcpTool[Any]]:
    @tool("plan", "Record the plan.", {"intent": str})
    async def plan(args: dict[str, Any]) -> dict[str, Any]:
        log.append(f"plan {args['intent']}")
        return {
            "content": [{"type": "text", "text": "plan_id: plan-1"}],
            "data": {"plan_id": "plan-1"},
        }

    @tool("run", "Run the plan.", {"plan_id": str})
    async def run(args: dict[str, Any]) -> dict[str, Any]:
        log.append(f"run {args['plan_id']}")
        if args["plan_id"] != "plan-1":
            return {"content": [{"type": "text", "text": "Refused: plan first"}], "is_error": True}
        return {"content": [{"type": "text", "text": "347"}], "data": {"rows": [[347]]}}

    @tool("boom", "Fail.", {})
    async def boom(args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("the source went away")

    @tool("review", "Ask the critic.", {})
    async def review(args: dict[str, Any]) -> dict[str, Any]:
        assert gateway is not None
        verdict = await gateway.text("answer_review", "You review SQL.", "SELECT 347")
        return {"content": [{"type": "text", "text": verdict or "no verdict"}]}

    return [plan, run, boom, review]


class Harness:
    def __init__(self, tmp_path: Path, scripts: list[list[Any]], *, concurrency: int = 3) -> None:
        self.query = FakeQuery(scripts)
        self.calls = MemoryCalls()
        self.sandbox = tmp_path / "claude-sandbox"
        self.cli = fake_claude(tmp_path)
        self.gateway = ClaudeGateway(
            calls=self.calls,
            sandbox=self.sandbox,
            cli_path=self.cli,
            concurrency=concurrency,
            query=self.query,
        )
        self.log: list[str] = []
        self.tools = tools(self.log, self.gateway)

    def start(self, max_turns: int = 16) -> AgentRun[Answer]:
        return self.gateway.agent("answer_agent", SYSTEM, USER, self.tools, Answer, max_turns)

    def run(self) -> AgentRun[Answer]:
        async def go() -> AgentRun[Answer]:
            return await self.start().wait()

        return asyncio.run(go())


ANSWER = {"answer": "347 albums.", "plan_id": "plan-1"}


def session(*items: Any, output: Any = ANSWER, model: str = OPUS) -> list[Any]:
    return [init(model), limit(), *items, said("Done.", model=model), result(structured=output)]


def test_the_session_runs_with_the_tools_and_the_answer_schema(tmp_path: Path) -> None:
    h = Harness(tmp_path, [session()])
    h.run()
    [spawn] = h.query.spawns
    o = spawn.options
    assert spawn.prompt == USER and o.system_prompt == SYSTEM
    assert o.cli_path == h.cli and o.cwd == h.sandbox
    assert o.setting_sources == [] and o.tools == []
    assert o.permission_mode == "dontAsk"
    assert o.allowed_tools == [
        "mcp__ontological__plan",
        "mcp__ontological__run",
        "mcp__ontological__boom",
        "mcp__ontological__review",
    ]
    assert list(o.mcp_servers) == ["ontological"]
    assert o.output_format == {"type": "json_schema", "schema": Answer.model_json_schema()}
    assert (o.model, o.effort, o.fallback_model) == ("opus", "medium", "sonnet")  # §6.3
    assert o.max_turns == 16
    assert o.env == call_env()
    assert o.extra_args == {"no-session-persistence": None}


def test_tool_calls_run_the_handlers_and_are_logged_in_order(tmp_path: Path) -> None:
    h = Harness(
        tmp_path,
        [
            session(
                Call("run", {"plan_id": "plan-0"}),
                Call("plan", {"intent": "count"}),
                Call("run", {"plan_id": "plan-1"}),
            )
        ],
    )
    run = h.run()

    assert h.log == ["run plan-0", "plan count", "run plan-1"]
    assert run.tool_log == [
        ToolCall("run", {"plan_id": "plan-0"}, True, "Refused: plan first", {}),
        ToolCall("plan", {"intent": "count"}, False, "plan_id: plan-1", {"plan_id": "plan-1"}),
        ToolCall("run", {"plan_id": "plan-1"}, False, "347", {"rows": [[347]]}),
    ]
    # The model sees the content and the error flag, not the data kept for the check
    assert h.query.results[0] == {
        "content": [{"type": "text", "text": "Refused: plan first"}],
        "isError": True,
    }
    assert "data" not in h.query.results[1]


def test_a_tool_the_session_does_not_allow_never_runs(tmp_path: Path) -> None:
    h = Harness(tmp_path, [session(Call("Bash", {"command": "ls"}))])
    run = h.run()
    assert h.query.results == [{"denied": "mcp__ontological__Bash"}]
    assert run.tool_log == []


def test_arguments_that_fail_the_input_schema_are_refused_by_the_sdk(tmp_path: Path) -> None:
    h = Harness(tmp_path, [session(Call("plan", {"intent": 3}))])
    run = h.run()
    assert h.query.results[0]["isError"] is True
    assert h.log == [] and run.tool_log == []


def test_a_handler_that_raises_is_logged_and_the_model_told(tmp_path: Path) -> None:
    h = Harness(tmp_path, [session(Call("boom"))])
    run = h.run()
    [call] = run.tool_log
    assert call.name == "boom" and call.is_error
    assert "the source went away" in call.text
    assert h.query.results[0]["isError"] is True
    assert "the source went away" in h.query.results[0]["content"][0]["text"]


def test_events_arrive_as_the_session_goes(tmp_path: Path) -> None:
    h = Harness(tmp_path, [session(said("Planning."), Call("plan", {"intent": "count"}))])

    async def go() -> list[Any]:
        return [event async for event in h.start()]

    events = asyncio.run(go())
    assert [type(e) for e in events] == [Said, ToolCall, Said]
    assert events[0] == Said("Planning.") and events[2] == Said("Done.")


def test_the_answer_is_validated_and_the_answering_model_recorded(tmp_path: Path) -> None:
    h = Harness(tmp_path, [session(Call("plan", {"intent": "count"}))])
    run = h.run()

    assert run.output == Answer(answer="347 albums.", plan_id="plan-1")
    assert run.outcome == "ok" and run.error is None
    assert run.answered_model == OPUS
    [row] = h.calls.rows
    assert (row.task, row.kind, row.outcome, row.attempt) == ("answer_agent", "agent", "ok", 1)
    assert (row.model, row.effort, row.answered_model) == ("opus", "medium", OPUS)
    assert row.output == ANSWER
    assert not row.cacheable


def test_a_fallback_answer_records_the_fallback_model(tmp_path: Path) -> None:
    h = Harness(tmp_path, [[init(OPUS), fell_back(), *session(model=SONNET)[1:]]])
    run = h.run()
    assert run.answered_model == SONNET
    assert h.calls.rows[0].fallback


def test_an_answer_that_fails_the_schema_is_invalid_and_not_retried(tmp_path: Path) -> None:
    h = Harness(tmp_path, [session(output={"answer": "347"})])
    run = h.run()
    assert run.output is None and run.outcome == "invalid"
    assert run.error is not None and "plan_id" in run.error
    assert len(h.query.spawns) == 1


def test_running_out_of_turns_is_invalid(tmp_path: Path) -> None:
    out_of_turns = result(is_error=True, subtype="error_max_turns")
    h = Harness(tmp_path, [[init(OPUS), Call("plan", {"intent": "x"}), out_of_turns]])
    run = h.run()
    assert run.output is None and run.outcome == "invalid"
    assert run.error is not None and "error_max_turns" in run.error
    assert len(run.tool_log) == 1  # the log of what did happen is kept


def test_a_rate_limit_is_limited(tmp_path: Path) -> None:
    rejected = limit("rejected")
    h = Harness(tmp_path, [[init(OPUS), rejected, result(is_error=True, api_error_status=429)]])
    run = h.run()
    assert run.output is None and run.outcome == "limited"
    assert h.gateway.status.state == "limited"


def test_a_session_is_never_served_from_the_cache(tmp_path: Path) -> None:
    h = Harness(tmp_path, [session(), session()])
    h.run()
    h.run()
    assert len(h.query.spawns) == 2


def test_a_call_from_inside_a_tool_does_not_wait_for_the_sessions_own_slot(
    tmp_path: Path,
) -> None:
    review = answered(text="Looks right.", model=SONNET)
    h = Harness(tmp_path, [session(Call("review")), review], concurrency=1)

    async def go() -> AgentRun[Answer]:
        return await asyncio.wait_for(h.start().wait(), timeout=5)

    run = asyncio.run(go())
    assert run.tool_log[0].text == "Looks right."
    assert [r.task for r in h.calls.rows] == ["answer_review", "answer_agent"]
