"""Contract tests for the Agent SDK behaviour the gateway and the gated agent rely on (spike S1).

Every test but the parser test spawns the user's logged-in `claude` and spends a little quota,
so they run only when ONTOLOGICAL_CONTRACT=1. ONTOLOGICAL_CONTRACT_MODEL picks the model
(default `haiku`). With S1_REPORT=<path>, the sessions' observations are written there as JSON
for tools/spikes/s1_agent_sdk.py to print and compare.

See docs/ARCHITECTURE.md §3, §6.2 and §8.1, and docs/BUILD_PLAN.md spike S1.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookEventMessage,
    McpSdkServerConfig,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from claude_agent_sdk._internal.message_parser import (  # pyright: ignore[reportPrivateUsage]
    parse_message,
)

CONTRACT = os.environ.get("ONTOLOGICAL_CONTRACT") == "1"
MODEL = os.environ.get("ONTOLOGICAL_CONTRACT_MODEL", "haiku")
REPORT = os.environ.get("S1_REPORT")

needs_claude = pytest.mark.skipif(
    not CONTRACT, reason="spawns claude and spends quota; set ONTOLOGICAL_CONTRACT=1"
)

# Variables a Claude Code session sets for its children. An engine started by the MCP shim
# inherits them; the report lists which ones were present for a run.
SESSION_VAR_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PID", "CLAUDE_EFFORT")

SERVER = "onto"
PLAN_ID = "p-7"
ANSWER_CODE = "KESTREL-41"
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"code": {"type": "string"}, "plan_id": {"type": "string"}},
    "required": ["code", "plan_id"],
    "additionalProperties": False,
}
BOGUS_API_KEY = "sk-ant-api03-ontological-contract-test-not-a-key"


def _text(text: str, *, error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        result["is_error"] = True
    return result


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    refused: bool


@dataclass
class Run:
    """Everything one `query()` session yielded, sorted by kind."""

    init: dict[str, Any] = field(default_factory=dict[str, Any])
    system: list[SystemMessage] = field(default_factory=list[SystemMessage])
    hook_events: list[HookEventMessage] = field(default_factory=list[HookEventMessage])
    rate_limits: list[RateLimitEvent] = field(default_factory=list[RateLimitEvent])
    assistant_models: list[str] = field(default_factory=list[str])
    result: ResultMessage | None = None
    error: str | None = None
    tool_log: list[ToolCall] = field(default_factory=list[ToolCall])

    def system_of(self, subtype: str) -> list[dict[str, Any]]:
        return [m.data for m in self.system if m.subtype == subtype]

    def report(self) -> dict[str, Any]:
        init_keys = ("model", "tools", "mcp_servers", "permissionMode", "apiKeySource")
        init_keys += ("plugins", "skills", "output_style", "claude_code_version")
        result = self.result
        return {
            "init": {k: self.init.get(k) for k in init_keys},
            "init_session_id": self.init.get("session_id"),
            "system_subtypes": sorted({m.subtype for m in self.system}),
            "hook_events": len(self.hook_events),
            "rate_limits": [e.rate_limit_info.raw for e in self.rate_limits],
            "assistant_models": sorted(set(self.assistant_models)),
            "tool_log": [(c.name, c.args, c.refused) for c in self.tool_log],
            "result": None
            if result is None
            else {
                "subtype": result.subtype,
                "is_error": result.is_error,
                "structured_output": result.structured_output,
                "model_usage": result.model_usage,
                "permission_denials": result.permission_denials,
                "num_turns": result.num_turns,
                "duration_ms": result.duration_ms,
            },
            "error": self.error,
        }


def _gated_server(log: list[ToolCall]) -> McpSdkServerConfig:
    """A miniature of §8.1: `run` refuses until `plan` has issued the plan_id it is given."""

    @tool("plan", "Record what you intend to do. Returns a plan_id.", {"intent": str})
    async def plan(args: dict[str, Any]) -> dict[str, Any]:
        log.append(ToolCall("plan", args, refused=False))
        return _text(f"plan_id: {PLAN_ID}")

    @tool("run", "Run a plan and return the answer code.", {"plan_id": str})
    async def run(args: dict[str, Any]) -> dict[str, Any]:
        plan_id = args.get("plan_id")
        if plan_id != PLAN_ID:
            log.append(ToolCall("run", args, refused=True))
            return _text(
                f"Refused: unknown plan_id {plan_id!r}. "
                "Call plan(intent) first and pass the plan_id it returns.",
                error=True,
            )
        log.append(ToolCall("run", args, refused=False))
        return _text(f"answer code: {ANSWER_CODE}")

    @tool("audit", "Write an audit note.", {"note": str})
    async def audit(args: dict[str, Any]) -> dict[str, Any]:
        log.append(ToolCall("audit", args, refused=False))
        return _text("ok")

    return create_sdk_mcp_server(SERVER, tools=[plan, run, audit])


def _locked_down(**overrides: Any) -> ClaudeAgentOptions:
    """The §6.2 options every gateway call shares."""
    options: dict[str, Any] = {
        "cwd": tempfile.mkdtemp(prefix="ontological-sandbox-"),
        "setting_sources": [],
        "tools": [],
        "permission_mode": "dontAsk",
        "model": MODEL,
        "effort": "low",
        "max_turns": 1,
        "include_hook_events": True,
    }
    options.update(overrides)
    return ClaudeAgentOptions(**options)


async def _collect(prompt: str, options: ClaudeAgentOptions, run: Run) -> Run:
    try:
        async for message in query(prompt=prompt, options=options):
            match message:
                case HookEventMessage():
                    run.hook_events.append(message)
                case SystemMessage(subtype="init"):
                    run.init = message.data
                case SystemMessage():
                    run.system.append(message)
                case RateLimitEvent():
                    run.rate_limits.append(message)
                case AssistantMessage():
                    run.assistant_models.append(message.model)
                case ResultMessage():
                    run.result = message
                case _:
                    pass
    except Exception as exc:  # the SDK raises after yielding an error result
        run.error = f"{type(exc).__name__}: {exc}"
    return run


def _gated_session() -> Run:
    run = Run()
    options = _locked_down(
        system_prompt=(
            "You answer by calling tools. A tool may refuse with an error that says which "
            "step is missing; when it does, do that step and try again."
        ),
        mcp_servers={SERVER: _gated_server(run.tool_log)},
        allowed_tools=[f"mcp__{SERVER}__plan", f"mcp__{SERVER}__run"],
        output_format={"type": "json_schema", "schema": ANSWER_SCHEMA},
        max_turns=10,
    )
    prompt = (
        "First call the audit tool once with note 'start' (if it fails, carry on). "
        "Then use the run tool with plan_id 'p-0' to fetch the answer code, and return it."
    )
    return asyncio.run(_collect(prompt, options, run))


def _fallback_session() -> Run:
    options = _locked_down(model="claude-ontological-no-such-model", fallback_model=MODEL)
    return asyncio.run(_collect("Reply with the word OK.", options, Run()))


def _api_key_session(*, override: bool) -> Run:
    """Run with a bogus ANTHROPIC_API_KEY inherited, optionally blanked through `env`."""
    saved = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = BOGUS_API_KEY
    try:
        env = {"ANTHROPIC_API_KEY": ""} if override else {}
        return asyncio.run(_collect("Reply with the word OK.", _locked_down(env=env), Run()))
    finally:
        if saved is None:
            del os.environ["ANTHROPIC_API_KEY"]
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved


_REPORT: dict[str, Any] = {}


def _recorded(name: str, run: Run) -> Run:
    _REPORT[name] = run.report()
    return run


@pytest.fixture(scope="module", autouse=True)
def _write_report() -> Iterator[None]:
    yield
    if REPORT and _REPORT:
        _REPORT["model"] = MODEL
        _REPORT["session_vars"] = sorted(
            k for k in os.environ if k.startswith(SESSION_VAR_PREFIXES)
        )
        Path(REPORT).write_text(json.dumps(_REPORT, indent=2, default=str))


@pytest.fixture(scope="module")
def gated() -> Run:
    return _recorded("gated", _gated_session())


@pytest.fixture(scope="module")
def fallback() -> Run:
    return _recorded("fallback", _fallback_session())


@pytest.fixture(scope="module")
def api_key_blanked() -> Run:
    return _recorded("api_key_blanked", _api_key_session(override=True))


@pytest.fixture(scope="module")
def api_key_inherited() -> Run:
    return _recorded("api_key_inherited", _api_key_session(override=False))


def _result(run: Run) -> ResultMessage:
    assert run.result is not None, run.error
    return run.result


@needs_claude
def test_tool_error_is_read_and_recovered_from(gated: Run) -> None:
    calls = [(c.name, c.refused) for c in gated.tool_log if c.name != "audit"]
    assert calls[0] == ("run", True), calls
    assert ("plan", False) in calls
    assert calls[-1] == ("run", False), calls
    assert calls.index(("plan", False)) < len(calls) - 1


@needs_claude
def test_output_format_json_schema_works_alongside_tools(gated: Run) -> None:
    result = _result(gated)
    assert result.subtype == "success"
    assert result.structured_output == {"code": ANSWER_CODE, "plan_id": PLAN_ID}
    assert any(not c.refused for c in gated.tool_log)


@needs_claude
def test_init_message_reports_the_resolved_model(gated: Run) -> None:
    model = cast(str, gated.init["model"])
    assert model.startswith("claude-"), model
    if MODEL in ("haiku", "sonnet", "opus", "fable"):
        assert MODEL in model and model != MODEL
    assert set(gated.assistant_models) == {model}


@needs_claude
def test_result_reports_usage_per_model(gated: Run) -> None:
    usage = _result(gated).model_usage
    assert usage is not None
    entry = usage[cast(str, gated.init["model"])]
    assert entry["inputTokens"] > 0
    assert entry["outputTokens"] > 0
    assert isinstance(entry["costUSD"], float)


@needs_claude
def test_rate_limit_events_arrive_and_parse(gated: Run) -> None:
    assert gated.rate_limits, "no rate_limit_event in the stream"
    for event in gated.rate_limits:
        info = event.rate_limit_info
        assert info.status in ("allowed", "allowed_warning", "rejected")
        assert info.resets_at is None or isinstance(info.resets_at, int)


def test_rate_limit_event_parses_the_cli_wire_shape() -> None:
    # The shape claude 2.1.318 sends. Utilization is per window, under `unifiedWindows`;
    # the top-level `utilization` the SDK models is absent, so the parsed field is None.
    wire: dict[str, Any] = {
        "type": "rate_limit_event",
        "uuid": "u-1",
        "session_id": "s-1",
        "rate_limit_info": {
            "status": "allowed_warning",
            "resetsAt": 1794240000,
            "rateLimitType": "seven_day",
            "overageStatus": "rejected",
            "overageDisabledReason": "org_level_disabled",
            "isUsingOverage": False,
            "unifiedWindows": {
                "seven_day": {"utilization": 0.91, "resetsAt": 1794240000, "status": "allowed"}
            },
        },
    }
    event = parse_message(wire)
    assert isinstance(event, RateLimitEvent)
    info = event.rate_limit_info
    assert info.status == "allowed_warning"
    assert info.resets_at == 1794240000
    assert info.rate_limit_type == "seven_day"
    assert info.utilization is None
    assert info.raw["unifiedWindows"]["seven_day"]["utilization"] == 0.91


@needs_claude
def test_dont_ask_denies_tools_outside_allowed_tools(gated: Run) -> None:
    assert gated.init["permissionMode"] == "dontAsk"
    assert not any(c.name == "audit" for c in gated.tool_log), "audit ran"
    denials = _result(gated).permission_denials or []
    assert [d["tool_name"] for d in denials] == [f"mcp__{SERVER}__audit"]
    assert _result(gated).subtype == "success"


@needs_claude
def test_empty_setting_sources_skips_user_settings(gated: Run) -> None:
    plugins = cast(list[dict[str, Any]], gated.init["plugins"])
    assert all(p["path"] == "builtin" for p in plugins), plugins
    servers = cast(list[dict[str, Any]], gated.init["mcp_servers"])
    assert [(s["name"], s.get("source")) for s in servers] == [(SERVER, "sdk")]
    assert not gated.hook_events


@needs_claude
def test_empty_tools_leaves_only_our_tools(gated: Run) -> None:
    tools = cast(list[str], gated.init["tools"])
    builtin = [t for t in tools if not t.startswith(f"mcp__{SERVER}__")]
    assert builtin == ["StructuredOutput"], tools


@needs_claude
def test_fallback_model_answers_when_the_model_is_unavailable(fallback: Run) -> None:
    result = _result(fallback)
    assert result.subtype == "success" and not result.is_error
    [switch] = fallback.system_of("model_fallback")
    assert switch["trigger"] == "model_not_found"
    assert set(fallback.assistant_models) == {switch["fallback_model"]}


@needs_claude
def test_inherited_api_key_is_used_unless_blanked(
    api_key_inherited: Run, api_key_blanked: Run
) -> None:
    # options.env is merged over os.environ and can't remove a key; an empty value unsets it.
    assert api_key_inherited.init["apiKeySource"] == "ANTHROPIC_API_KEY"
    assert _result(api_key_inherited).is_error
    assert api_key_blanked.init["apiKeySource"] == "none"
    assert not _result(api_key_blanked).is_error


@needs_claude
def test_spawned_session_is_not_the_parent_session(gated: Run) -> None:
    parent = os.environ.get("CLAUDE_CODE_SESSION_ID")
    assert gated.init["session_id"] != parent
    assert "ontological-sandbox-" in gated.init["cwd"]
