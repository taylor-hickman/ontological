"""Stand-ins for what the gateway talks to: the SDK's `query()`, the `claude` binary's
`--version`, and `ops.llm_call`.

`FakeQuery` plays back a script of real SDK message objects, one script per spawn, and records
each spawn's prompt and options. A `Call` in a script calls one of the session's SDK MCP tools
the way Claude Code does, through the SDK's own JSON-RPC bridge, so the tool's input schema and
result shaping are the SDK's. The message builders give the shapes Claude Code 2.1.318 sends
(spike S1).
"""

from __future__ import annotations

import asyncio
import stat
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    McpSdkServerConfig,
    Message,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    TextBlock,
)
from claude_agent_sdk._internal.sdk_mcp_bridge import (  # pyright: ignore[reportPrivateUsage]
    SdkMcpBridge,
)
from claude_agent_sdk.types import (
    AssistantMessageError,
    ModelUsage,
    RateLimitStatus,
    RateLimitType,
)

from ontological.llm.calls import LlmCall

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
RESETS_AT = 1794240000


def init(model: str = HAIKU) -> SystemMessage:
    return SystemMessage("init", {"model": model, "session_id": "s-1", "apiKeySource": "none"})


def fell_back(to: str = SONNET, frm: str = OPUS) -> SystemMessage:
    return SystemMessage(
        "model_fallback",
        {"original_model": frm, "fallback_model": to, "trigger": "overloaded"},
    )


def limit(
    status: RateLimitStatus = "allowed",
    *,
    window: RateLimitType = "seven_day",
    utilization: float = 0.4,
    resets_at: int | None = RESETS_AT,
) -> RateLimitEvent:
    raw: dict[str, Any] = {
        "status": status,
        "resetsAt": resets_at,
        "rateLimitType": window,
        "unifiedWindows": {
            window: {"utilization": utilization, "resetsAt": resets_at, "status": status}
        },
    }
    info = RateLimitInfo(status=status, resets_at=resets_at, rate_limit_type=window, raw=raw)
    return RateLimitEvent(rate_limit_info=info, uuid="u-1", session_id="s-1")


def said(
    text: str = "ok", *, model: str = HAIKU, error: AssistantMessageError | None = None
) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text)], model=model, error=error)


def usage(inputs: int, outputs: int, cost: float) -> ModelUsage:
    """A model_usage entry, with the fields the gateway's tests read."""
    return cast(ModelUsage, {"inputTokens": inputs, "outputTokens": outputs, "costUSD": cost})


def result(
    *,
    structured: Any = None,
    text: str | None = None,
    is_error: bool = False,
    subtype: str = "success",
    api_error_status: int | None = None,
    model: str = HAIKU,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1200,
        duration_api_ms=900,
        is_error=is_error,
        num_turns=1,
        session_id="s-1",
        total_cost_usd=0.0004,
        result=text,
        structured_output=structured,
        model_usage={model: usage(120, 30, 0.0004)},
        api_error_status=api_error_status,
    )


def answered(structured: Any = None, text: str | None = None, model: str = HAIKU) -> list[Any]:
    """A successful session: init, a rate-limit event, the model's turn and the result."""
    return [
        init(model),
        limit(),
        said(model=model),
        result(structured=structured, text=text, model=model),
    ]


@dataclass(frozen=True)
class Call:
    """A script item: the model calls `tool` with `args`. FakeQuery appends what the tool sent
    back, as the CLI receives it (`content`, `isError`), to `FakeQuery.results`."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict[str, Any])
    server: str = "ontological"


@dataclass
class Spawn:
    prompt: str
    options: ClaudeAgentOptions


@dataclass
class FakeQuery:
    """`query()` for a script per spawn. A script item that is an exception is raised there,
    the way the SDK raises ResultError after yielding an error result; a float is a pause of that
    many seconds; a `Call` calls a tool. `peak` is the most spawns that were in flight at once."""

    scripts: list[Sequence[Any]]
    spawns: list[Spawn] = field(default_factory=list[Spawn])
    results: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    in_flight: int = 0
    peak: int = 0

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        assert self.scripts, f"an unexpected spawn: {prompt!r}"
        self.spawns.append(Spawn(prompt, options))
        return self._play(self.scripts.pop(0), options)

    async def _play(
        self, script: Sequence[Any], options: ClaudeAgentOptions
    ) -> AsyncIterator[Message]:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        bridges: dict[str, SdkMcpBridge] = {}
        try:
            for item in script:
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, float):
                    await asyncio.sleep(item)
                    continue
                if isinstance(item, Call):
                    self.results.append(await self._call(item, options, bridges))
                    continue
                yield item
        finally:
            self.in_flight -= 1
            for bridge in bridges.values():
                await bridge.aclose()

    async def _call(
        self, call: Call, options: ClaudeAgentOptions, bridges: dict[str, SdkMcpBridge]
    ) -> dict[str, Any]:
        servers = cast(dict[str, McpSdkServerConfig], options.mcp_servers)
        if call.server not in bridges:
            bridge = bridges[call.server] = SdkMcpBridge(
                call.server, servers[call.server]["instance"]
            )
            await bridge.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "fake-claude", "version": "0"},
                    },
                }
            )
            await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        name = f"mcp__{call.server}__{call.tool}"
        if name not in (options.allowed_tools or []):
            # Claude Code denies it under dontAsk; the handler never runs (spike S1)
            return {"denied": name}
        reply = await bridges[call.server].handle(
            {
                "jsonrpc": "2.0",
                "id": len(self.results) + 1,
                "method": "tools/call",
                "params": {"name": call.tool, "arguments": call.args},
            }
        )
        assert reply is not None and "result" in reply, reply
        return cast(dict[str, Any], reply["result"])


@dataclass
class MemoryCalls:
    """ops.llm_call in a list, with PgCallLog's lookup rule."""

    rows: list[LlmCall] = field(default_factory=list[LlmCall])

    def lookup(self, cache_key: str) -> Any | None:
        for row in reversed(self.rows):
            if row.cache_key == cache_key and row.cacheable:
                return row.output
        return None

    def record(self, call: LlmCall) -> None:
        self.rows.append(call)


def fake_claude(directory: Path, version_line: str = "2.1.318 (Claude Code)") -> Path:
    """An executable `claude` that only answers --version."""
    path = directory / "claude"
    path.write_text(f'#!/bin/sh\n[ "$1" = --version ] && echo "{version_line}" && exit 0\nexit 2\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path
