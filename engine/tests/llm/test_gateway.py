"""ClaudeGateway.structured() and text() (ARCHITECTURE.md §6.1 to §6.5), against a scripted
`query()`, so no `claude` is spawned and no quota is spent. tests/contract checks the same
behaviour against the real binary."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import CLINotFoundError, ResultError
from pydantic import BaseModel
from sdk_fakes import (
    HAIKU,
    OPUS,
    RESETS_AT,
    SONNET,
    FakeQuery,
    MemoryCalls,
    answered,
    fake_claude,
    fell_back,
    init,
    limit,
    result,
    said,
    usage,
)

from ontological.llm import gateway as gateway_module
from ontological.llm.calls import LlmCall, cache_key
from ontological.llm.env import call_env
from ontological.llm.gateway import ClaudeGateway, GatewayStatus
from ontological.llm.routes import Route

NOW = RESETS_AT - 3600
SYSTEM = "You find primary keys."
USER = "table orders(id, customer_id, total)"


class Keys(BaseModel):
    pk: list[str]


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        scripts: Sequence[Sequence[Any]] = (),
        *,
        routes: dict[str, Route] | None = None,
        cli_path: Path | None = None,
        concurrency: int = 3,
        calls: MemoryCalls | None = None,
    ) -> None:
        self.now = float(NOW)
        self.query = FakeQuery(list(scripts))
        self.calls = calls or MemoryCalls()
        self.sandbox = tmp_path / "claude-sandbox"
        self.cli = cli_path or fake_claude(tmp_path)
        self.gateway = ClaudeGateway(
            calls=self.calls,
            sandbox=self.sandbox,
            cli_path=self.cli,
            routes=routes,
            concurrency=concurrency,
            query=self.query,
            clock=lambda: self.now,
        )

    def structured(self, task: str = "table_keys", **kw: Any) -> BaseModel | None:
        return asyncio.run(self.gateway.structured(task, SYSTEM, USER, Keys, **kw))

    def text(self, task: str = "answer_review") -> str | None:
        return asyncio.run(self.gateway.text(task, SYSTEM, USER))

    @property
    def status(self) -> GatewayStatus:
        return self.gateway.status

    @property
    def rows(self) -> list[LlmCall]:
        return self.calls.rows


# --- a call -------------------------------------------------------------------------------------


def test_structured_returns_the_validated_model(tmp_path: Path) -> None:
    h = Harness(tmp_path, [answered({"pk": ["id"]}, model=SONNET)])
    assert h.structured() == Keys(pk=["id"])


def test_structured_runs_with_the_6_2_options(tmp_path: Path) -> None:
    h = Harness(tmp_path, [answered({"pk": ["id"]}, model=SONNET)])
    h.structured()
    [spawn] = h.query.spawns
    o = spawn.options
    assert spawn.prompt == USER
    assert o.system_prompt == SYSTEM
    assert o.cli_path == h.cli
    assert o.cwd == h.sandbox and h.sandbox.is_dir() and not any(h.sandbox.iterdir())
    assert o.setting_sources == []
    assert o.tools == []
    assert o.allowed_tools == []
    assert o.permission_mode == "dontAsk"
    assert o.output_format == {"type": "json_schema", "schema": Keys.model_json_schema()}
    assert (o.model, o.effort) == ("sonnet", "high")  # table_keys, §6.3
    assert o.fallback_model is None
    assert o.max_turns == 2
    assert o.extra_args == {"no-session-persistence": None}
    assert o.mcp_servers == {}


def test_every_call_blanks_the_auth_variables_even_when_the_shell_sets_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-stray")
    h = Harness(tmp_path, [answered({"pk": ["id"]})])
    h.structured()
    env = h.query.spawns[0].options.env
    assert env == call_env()
    assert env["ANTHROPIC_API_KEY"] == ""


def test_opus_routes_set_the_sonnet_fallback(tmp_path: Path) -> None:
    h = Harness(tmp_path, [answered({"pk": ["id"]}, model=OPUS)])
    h.structured("bridge_sql")
    o = h.query.spawns[0].options
    assert (o.model, o.effort, o.fallback_model) == ("opus", "high", "sonnet")


def test_a_route_with_a_full_model_id_is_passed_as_written(tmp_path: Path) -> None:
    routes = {"table_keys": Route("claude-opus-5-5", "max")}
    h = Harness(tmp_path, [answered({"pk": ["id"]}, model=OPUS)], routes=routes)
    h.structured()
    o = h.query.spawns[0].options
    assert (o.model, o.effort, o.fallback_model) == ("claude-opus-5-5", "max", "sonnet")


def test_an_unknown_task_is_a_programming_error(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    with pytest.raises(ValueError, match="no_such_task"):
        h.structured("no_such_task")


def test_concurrency_bounds_the_claude_processes_in_flight(tmp_path: Path) -> None:
    scripts = [[0.05, *answered({"pk": ["id"]})] for _ in range(5)]
    h = Harness(tmp_path, scripts, concurrency=2)

    async def five() -> list[BaseModel | None]:
        return await asyncio.gather(
            *(h.gateway.structured("table_keys", SYSTEM, f"table t{i}", Keys) for i in range(5))
        )

    assert asyncio.run(five()) == [Keys(pk=["id"])] * 5
    assert h.query.peak == 2


# --- ops.llm_call and the cache -----------------------------------------------------------------


def test_a_call_is_logged_with_the_route_and_the_answering_model(tmp_path: Path) -> None:
    h = Harness(tmp_path, [answered({"pk": ["id"]}, model=SONNET)])
    h.structured(prompt_version="3", cache_key_extra="sample-1")
    [row] = h.rows
    assert row.task == "table_keys" and row.kind == "structured"
    assert (row.model, row.effort, row.prompt_version) == ("sonnet", "high", "3")
    assert row.cache_key == cache_key(
        task="table_keys",
        prompt_version="3",
        model="sonnet",
        effort="high",
        system=SYSTEM,
        user=USER,
        schema=Keys,
        extra="sample-1",
    )
    assert (row.resolved_model, row.answered_model, row.fallback) == (SONNET, SONNET, False)
    assert (row.attempt, row.outcome, row.error) == (1, "ok", None)
    assert row.output == {"pk": ["id"]}
    assert row.usage == {SONNET: {"inputTokens": 120, "outputTokens": 30, "costUSD": 0.0004}}
    assert (row.cost_usd, row.duration_ms, row.num_turns) == (0.0004, 1200, 1)
    assert row.session_id == "s-1"


def test_a_repeated_call_is_answered_from_the_cache(tmp_path: Path) -> None:
    h = Harness(tmp_path, [answered({"pk": ["id"]})])
    assert h.structured() == Keys(pk=["id"])
    assert h.structured() == Keys(pk=["id"])
    assert len(h.query.spawns) == 1
    assert len(h.rows) == 1


def test_a_different_effort_misses_the_cache(tmp_path: Path) -> None:
    h = Harness(tmp_path, [answered({"pk": ["id"]})])
    h.structured()
    low = {"table_keys": Route("sonnet", "low")}
    h2 = Harness(tmp_path, [answered({"pk": ["id"]})], routes=low, calls=h.calls)
    h2.structured()
    assert len(h2.query.spawns) == 1, "the low-effort call spawned"
    assert [r.effort for r in h.calls.rows] == ["high", "low"]


def test_an_answer_from_the_fallback_model_is_logged_as_such_and_not_cached(
    tmp_path: Path,
) -> None:
    fallback = [init(OPUS), limit(), fell_back(), said(model=SONNET), result(structured={"pk": []})]
    h = Harness(tmp_path, [fallback, answered({"pk": ["id"]}, model=OPUS)])
    assert h.structured("bridge_sql") == Keys(pk=[])
    row = h.rows[0]
    assert (row.model, row.resolved_model, row.answered_model) == ("opus", OPUS, SONNET)
    assert row.fallback and row.outcome == "ok"
    assert h.structured("bridge_sql") == Keys(pk=["id"]), "asked Opus again"
    assert len(h.query.spawns) == 2


def test_the_answering_model_ignores_claude_codes_side_calls_in_model_usage(
    tmp_path: Path,
) -> None:
    done = result(structured={"pk": ["id"]}, model=OPUS)
    assert done.model_usage is not None
    done.model_usage[HAIKU] = usage(50, 5, 0.00001)
    h = Harness(tmp_path, [[init(OPUS), said(model=OPUS), done]])
    h.structured("bridge_sql")
    assert h.rows[0].answered_model == OPUS


def test_text_is_logged_but_not_cached(tmp_path: Path) -> None:
    h = Harness(tmp_path, [answered(text="Looks right."), answered(text="Looks right!")])
    assert h.text() == "Looks right."
    assert h.text() == "Looks right!"
    o = h.query.spawns[0].options
    assert o.output_format is None
    assert o.max_turns == 1
    assert (o.model, o.effort) == ("sonnet", "medium")  # answer_review, §6.3
    assert [(r.kind, r.outcome, r.output) for r in h.rows] == [
        ("text", "ok", "Looks right."),
        ("text", "ok", "Looks right!"),
    ]


# --- the retry rule (§6.2) ----------------------------------------------------------------------


def test_missing_structured_output_is_retried_once_with_the_error(tmp_path: Path) -> None:
    missing = [init(), said(), result(subtype="error_max_structured_output_retries", is_error=True)]
    h = Harness(tmp_path, [missing, answered({"pk": ["id"]})])
    assert h.structured() == Keys(pk=["id"])
    first, second = h.query.spawns
    assert first.prompt == USER
    assert second.prompt.startswith(USER) and "structured_output" in second.prompt
    assert [(r.attempt, r.outcome) for r in h.rows] == [(1, "invalid"), (2, "ok")]


def test_output_that_fails_validation_is_retried_once_with_the_validation_error(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, [answered({"pk": "id"}), answered({"pk": ["id"]})])
    assert h.structured() == Keys(pk=["id"])
    retry = h.query.spawns[1].prompt
    assert retry.startswith(USER) and "pk" in retry and "list" in retry.lower()
    assert h.rows[0].outcome == "invalid" and "pk" in (h.rows[0].error or "")


def test_a_second_invalid_answer_returns_none(tmp_path: Path) -> None:
    h = Harness(tmp_path, [answered({"pk": "id"}), answered({"keys": []})])
    assert h.structured() is None
    assert len(h.query.spawns) == 2
    assert [r.outcome for r in h.rows] == ["invalid", "invalid"]


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            [
                init(),
                limit("rejected", utilization=1.0),
                said("API Error: rate limited", model="<synthetic>", error="rate_limit"),
                result(is_error=True, api_error_status=429, text="API Error: 429"),
                ResultError("rate limited", {"subtype": "success", "api_error_status": 429}),
            ],
            id="rate_limit",
        ),
        pytest.param(
            [
                init(),
                limit(),
                said("API Error: overloaded", model="<synthetic>", error="server_error"),
                result(is_error=True, api_error_status=529, text="API Error: 529 overloaded"),
                ResultError("overloaded", {"subtype": "success", "api_error_status": 529}),
            ],
            id="overloaded",
        ),
    ],
)
def test_rate_limit_and_overload_are_not_retried(tmp_path: Path, script: list[Any]) -> None:
    h = Harness(tmp_path, [script])
    assert h.structured() is None
    assert len(h.query.spawns) == 1
    assert [r.outcome for r in h.rows] == ["limited"]


def test_a_rejected_rate_limit_makes_the_gateway_limited_until_it_resets(tmp_path: Path) -> None:
    script = [
        init(),
        limit("rejected", utilization=1.0),
        said("API Error", model="<synthetic>", error="rate_limit"),
        result(is_error=True, api_error_status=429),
    ]
    h = Harness(tmp_path, [script])
    h.structured()
    assert h.status.state == "limited"
    assert h.status.limited_until == datetime.fromtimestamp(RESETS_AT, UTC)
    h.now = RESETS_AT + 1
    assert h.status.state != "limited"


def test_a_failed_login_returns_none_and_needs_login(tmp_path: Path) -> None:
    script = [
        init(),
        said("Not logged in", model="<synthetic>", error="authentication_failed"),
        result(is_error=True, text="Not logged in · Please run /login"),
        ResultError("Not logged in", {"subtype": "success", "is_error": True}),
    ]
    h = Harness(tmp_path, [script])
    assert h.structured() is None
    assert len(h.query.spawns) == 1
    assert h.status.logged_in is False and h.status.state == "needs_login"
    assert h.rows[0].outcome == "error"


def test_a_process_error_without_a_result_returns_none(tmp_path: Path) -> None:
    h = Harness(tmp_path, [[init(), ResultError("exit 1", {})]])
    assert h.structured() is None
    assert len(h.query.spawns) == 1
    assert h.rows[0].outcome == "error" and "exit 1" in (h.rows[0].error or "")


def test_a_missing_cli_returns_none_without_spawning(tmp_path: Path) -> None:
    h = Harness(tmp_path, [], cli_path=tmp_path / "no-claude-here")
    assert h.structured() is None
    assert h.query.spawns == []
    assert h.status.installed is False and h.status.state == "not_installed"


def test_the_sdk_not_finding_the_cli_means_not_installed(tmp_path: Path) -> None:
    h = Harness(tmp_path, [[CLINotFoundError("Claude Code not found")]])
    assert h.structured() is None
    assert h.status.state == "not_installed"


def test_without_an_override_the_cli_is_found_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = fake_claude(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    gateway = ClaudeGateway(calls=MemoryCalls(), sandbox=tmp_path / "s", query=FakeQuery([]))
    assert gateway.cli_path == cli


# --- rate limits and status (§6.5) --------------------------------------------------------------


def test_the_latest_rate_limit_event_is_tracked_with_per_window_utilization(
    tmp_path: Path,
) -> None:
    warned = [init(), limit("allowed_warning", window="five_hour", utilization=0.91), said()]
    h = Harness(
        tmp_path,
        [answered({"pk": ["a"]}), [*warned, result(structured={"pk": ["b"]})]],
    )
    h.structured()
    first = h.status.rate_limit
    assert first is not None and first.status == "allowed"
    assert first.utilization == {"seven_day": 0.4}
    asyncio.run(h.gateway.structured("table_keys", SYSTEM, "another table", Keys))
    latest = h.status.rate_limit
    assert latest is not None
    assert (latest.status, latest.window) == ("allowed_warning", "five_hour")
    assert latest.utilization == {"five_hour": 0.91}
    assert latest.resets_at == datetime.fromtimestamp(RESETS_AT, UTC)
    assert h.status.state == "ready"


def test_check_reports_the_version_and_a_ping(tmp_path: Path) -> None:
    h = Harness(tmp_path, [answered(text="OK")])
    assert h.status.state == "unknown"
    status = asyncio.run(h.gateway.check())
    assert status.installed and status.version == "2.1.318"
    assert status.logged_in and status.state == "ready"
    o = h.query.spawns[0].options
    assert (o.model, o.effort) == ("haiku", "low")  # the ping route
    assert [r.task for r in h.rows] == ["ping"]


def test_check_without_a_cli_does_not_ping(tmp_path: Path) -> None:
    h = Harness(tmp_path, [], cli_path=tmp_path / "no-claude-here")
    status = asyncio.run(h.gateway.check())
    assert status.installed is False and status.state == "not_installed"
    assert h.query.spawns == []


def test_check_flags_a_claude_older_than_the_tested_minimum(tmp_path: Path) -> None:
    old = fake_claude(tmp_path, "2.0.9 (Claude Code)")
    h = Harness(tmp_path, [answered(text="OK")], cli_path=old)
    status = asyncio.run(h.gateway.check())
    assert status.version == "2.0.9" and status.state == "outdated"


def test_check_reports_a_failed_login(tmp_path: Path) -> None:
    script = [
        init(),
        said("Not logged in", model="<synthetic>", error="authentication_failed"),
        result(is_error=True, text="Not logged in · Please run /login"),
    ]
    h = Harness(tmp_path, [script])
    status = asyncio.run(h.gateway.check())
    assert status.installed and status.logged_in is False and status.state == "needs_login"


def test_check_gives_up_on_a_claude_that_does_not_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hung = tmp_path / "hung" / "claude"
    hung.parent.mkdir()
    hung.write_text(f"#!/bin/sh\necho $$ > {tmp_path / 'pid'}\nexec sleep 30\n")
    hung.chmod(0o755)
    monkeypatch.setattr(gateway_module, "VERSION_TIMEOUT_S", 0.5)
    h = Harness(tmp_path, [], cli_path=hung)
    status = asyncio.run(h.gateway.check())
    assert status.state == "not_installed" and h.query.spawns == []
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_successful_answer_that_mentions_not_logged_in_is_kept(tmp_path: Path) -> None:
    answer = "Users who are not logged in only see the landing page."
    h = Harness(tmp_path, [answered(text=answer)])
    assert h.text() == answer
    assert h.rows[0].outcome == "ok"
    assert h.status.logged_in is True and h.status.state == "ready"


def test_a_structured_answer_whose_result_text_mentions_not_logged_in_is_kept(
    tmp_path: Path,
) -> None:
    script = [init(), said(), result(structured={"pk": ["id"]}, text="not logged in users")]
    h = Harness(tmp_path, [script])
    assert h.structured() == Keys(pk=["id"])
    assert h.status.state == "ready"


def test_an_answer_that_arrives_with_a_rejected_rate_limit_event_is_kept(tmp_path: Path) -> None:
    script = [init(), limit("rejected", utilization=1.0), said(), result(structured={"pk": []})]
    h = Harness(tmp_path, [script])
    assert h.structured() == Keys(pk=[])
    assert h.rows[0].outcome == "ok"
    assert h.status.state == "limited", "the event still says the window is used up"
