"""ClaudeGateway (ARCHITECTURE.md §6): the only code that spawns `claude`.

Callers name a task, never a model; the route table picks the model and effort (§6.3). Each call
is one `claude_agent_sdk.query()` with §6.2's options: the user's own `claude`, an empty
sandbox cwd, no settings, no built-in tools, deny-by-default permissions, no session
persistence, and the auth variables blanked. `structured()` keeps upstream's contract: it
returns None on any failure, so the caller falls back deterministically.

Every spawn is logged in ops.llm_call (`calls.CallLog`), and so is every cache hit, with the job
and pack the caller's `calls.call_scope` names and the model that answered, read from
the assistant messages and the `model_fallback` message, not from `model_usage`, which also
lists Claude Code's own side calls (spike S1). The latest rate-limit event, and what the
calls and `check()` have learned about the `claude` install, make up `status` (§6.5).

There is no warm pool: a spawn and close cost 0.53 s at p50 (spike S2).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    Message,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk.types import EffortLevel
from pydantic import BaseModel, ValidationError

from ontological.llm.calls import CallLog, Kind, LlmCall, Outcome, cache_key, current_scope
from ontological.llm.env import call_env
from ontological.llm.routes import Route, fallback_for, merge_routes

# The oldest `claude` the contract tests have passed on (spikes S1 and S2).
MIN_CLAUDE_VERSION = (2, 1, 318)
VERSION_TIMEOUT_S = 10
DEFAULT_CONCURRENCY = 3  # §6.4; a user setting
STRUCTURED_MAX_TURNS = 2  # output_format answers through a StructuredOutput tool call
TEXT_MAX_TURNS = 1

RETRY_NOTE = (
    "\n\nYour previous answer was rejected: {error}\n"
    "Answer again, and make the answer match the JSON schema exactly."
)
PING_SYSTEM = "You are a connectivity check. Do exactly what the user asks."
PING_USER = "Reply with the word OK."

SYNTHETIC_MODEL = "<synthetic>"  # the model Claude Code puts on the error messages it makes up
LIMIT_STATUSES = (429, 529)  # rate limited, overloaded: §6.5 handles both

State = Literal["unknown", "ready", "not_installed", "outdated", "needs_login", "limited"]


class Query(Protocol):
    """`claude_agent_sdk.query`, or a stand-in for tests."""

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]: ...


@dataclass(frozen=True)
class RateLimit:
    """The latest RateLimitEvent. Claude Code 2.1.318 leaves the SDK's `utilization` empty and
    sends it per window under `raw["unifiedWindows"]` (spike S1)."""

    status: str  # allowed | allowed_warning | rejected
    window: str | None  # the window the status is about: five_hour, seven_day, ...
    resets_at: datetime | None
    utilization: dict[str, float] = field(default_factory=dict[str, float])

    @classmethod
    def from_info(cls, info: RateLimitInfo) -> RateLimit:
        windows = cast(dict[str, Any], info.raw.get("unifiedWindows") or {})
        utilization: dict[str, float] = {}
        for name, window in windows.items():
            value = cast(dict[str, Any], window).get("utilization") if window else None
            if isinstance(value, int | float):
                utilization[name] = float(value)
        return cls(
            status=info.status,
            window=info.rate_limit_type,
            resets_at=_timestamp(info.resets_at),
            utilization=utilization,
        )


@dataclass(frozen=True)
class GatewayStatus:
    """What the UI shows about Claude: installed? logged in? limited until? utilization?"""

    state: State
    cli_path: Path | None
    installed: bool | None  # None until a call or check() has run
    version: str | None
    logged_in: bool | None
    rate_limit: RateLimit | None
    limited_until: datetime | None
    error: str | None  # the last call's error


@dataclass
class _Session:
    """What one spawn yielded."""

    resolved_model: str | None = None
    answered_model: str | None = None
    fallback_model: str | None = None
    errors: list[str] = field(default_factory=list[str])  # AssistantMessage.error values
    rejected: bool = False  # a rejected rate-limit event arrived
    result: ResultMessage | None = None
    exception: str | None = None
    cli_missing: bool = False


@dataclass(frozen=True)
class _Verdict:
    outcome: Outcome
    error: str | None = None
    output: Any = None


def resolve_cli(override: str | Path | None) -> Path | None:
    """The user's `claude`: the settings override when there is one, else a PATH lookup. Never
    the SDK's bundled binary (§3 rule 1)."""
    if override is not None:
        path = Path(override)
        return path if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which("claude")
    return Path(found) if found else None


class ClaudeGateway:
    def __init__(
        self,
        *,
        calls: CallLog,
        sandbox: Path,
        cli_path: str | Path | None = None,
        routes: Mapping[str, Route] | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        query: Query = claude_agent_sdk.query,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`sandbox` is <appData>/claude-sandbox, the empty cwd of every spawn; `cli_path` is the
        settings override; `routes` lays the user's routes over the defaults."""
        self.calls = calls
        self.sandbox = sandbox
        self.routes = merge_routes(routes)
        self._cli_override = cli_path
        self.cli_path = resolve_cli(cli_path)
        self._slots = asyncio.Semaphore(concurrency)
        self._query = query
        self._clock = clock
        self._installed: bool | None = None if self.cli_path else False
        self._version: str | None = None
        self._logged_in: bool | None = None
        self._rate_limit: RateLimit | None = None
        self._error: str | None = None
        # How the latest structured() call ended. None from structured() is upstream's "fall
        # back" signal; this says whether the model answered badly ("invalid") or Claude didn't
        # answer at all ("limited", "error"), which compile must not fall back on. Read it right
        # after the call: with calls in flight together it is the one that ended last.
        self.last_outcome: Outcome | None = None

    # --- calls ----------------------------------------------------------------------------------

    async def structured[M: BaseModel](
        self,
        task: str,
        system: str,
        user: str,
        schema: type[M],
        cache_key_extra: str = "",
        *,
        prompt_version: str = "",
    ) -> M | None:
        """A validated `schema`, from the cache when this exact call has a valid answer, or
        None. Output that is missing or fails validation is retried once with the error appended
        to the user turn; a rate limit, an overload or any other error is not retried (§6.2)."""
        route = self._route(task)
        key = cache_key(
            task=task,
            prompt_version=prompt_version,
            model=route.model,
            effort=route.effort,
            system=system,
            user=user,
            schema=schema,
            extra=cache_key_extra,
        )
        cached = await asyncio.to_thread(self.calls.lookup, key)
        if cached is not None:
            try:
                answer = schema.model_validate(cached)
            except ValidationError:
                pass  # can't happen while the schema is in the key; ask again if it does
            else:
                await self._record_hit(task, prompt_version, key, route)
                self.last_outcome = "ok"
                return answer
        output_format = {"type": "json_schema", "schema": schema.model_json_schema()}
        prompt = user
        for attempt in (1, 2):
            session = await self._spawn(route, system, prompt, output_format, STRUCTURED_MAX_TURNS)
            if session is None:
                self.last_outcome = "error"
                return None
            verdict = self._failure(session) or _validate(session, schema)
            await self._record(
                task, "structured", prompt_version, key, route, attempt, session, verdict
            )
            self.last_outcome = verdict.outcome
            if verdict.outcome == "ok":
                return schema.model_validate(verdict.output)
            if verdict.outcome != "invalid":
                return None
            prompt = user + RETRY_NOTE.format(error=verdict.error)
        return None

    async def text(
        self, task: str, system: str, user: str, *, prompt_version: str = ""
    ) -> str | None:
        """The model's text answer, or None. Logged, but never served from the cache."""
        route = self._route(task)
        key = cache_key(
            task=task,
            prompt_version=prompt_version,
            model=route.model,
            effort=route.effort,
            system=system,
            user=user,
            schema=None,
        )
        session = await self._spawn(route, system, user, None, TEXT_MAX_TURNS)
        if session is None:
            return None
        verdict = self._failure(session) or _text(session)
        await self._record(task, "text", prompt_version, key, route, 1, session, verdict)
        return cast(str, verdict.output) if verdict.outcome == "ok" else None

    # --- status ---------------------------------------------------------------------------------

    async def check(self) -> GatewayStatus:
        """Find `claude`, read its version, and ping it: a round trip on the `ping` route that
        shows whether the user is logged in (§6.5)."""
        self.cli_path = resolve_cli(self._cli_override)
        version = None if self.cli_path is None else await _cli_version(self.cli_path)
        self._installed = version is not None
        self._version = version
        if version is not None:
            await self.text("ping", PING_SYSTEM, PING_USER)
        return self.status

    @property
    def status(self) -> GatewayStatus:
        limit = self._rate_limit
        limited = (
            limit is not None
            and limit.status == "rejected"
            and (limit.resets_at is None or limit.resets_at.timestamp() > self._clock())
        )
        state: State
        if self._installed is False:
            state = "not_installed"
        elif self._version is not None and _version_tuple(self._version) < MIN_CLAUDE_VERSION:
            state = "outdated"
        elif self._logged_in is False:
            state = "needs_login"
        elif limited:
            state = "limited"
        elif self._logged_in:
            state = "ready"
        else:
            state = "unknown"
        return GatewayStatus(
            state=state,
            cli_path=self.cli_path,
            installed=self._installed,
            version=self._version,
            logged_in=self._logged_in,
            rate_limit=limit,
            limited_until=limit.resets_at if limited and limit else None,
            error=self._error,
        )

    # --- one spawn ------------------------------------------------------------------------------

    def _route(self, task: str) -> Route:
        try:
            return self.routes[task]
        except KeyError:
            raise ValueError(f"no route for task {task!r}") from None

    def _options(
        self, route: Route, system: str, output_format: dict[str, Any] | None, max_turns: int
    ) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cli_path=self.cli_path,
            cwd=self.sandbox,
            setting_sources=[],
            tools=[],
            allowed_tools=[],
            permission_mode="dontAsk",
            system_prompt=system,
            model=route.model,
            effort=cast(EffortLevel, route.effort),
            fallback_model=fallback_for(route.model),
            max_turns=max_turns,
            output_format=output_format,
            env=call_env(),
            extra_args={"no-session-persistence": None},
        )

    async def _spawn(
        self,
        route: Route,
        system: str,
        prompt: str,
        output_format: dict[str, Any] | None,
        max_turns: int,
    ) -> _Session | None:
        """Run one `query()`; None, without spawning, when there is no `claude` to run."""
        if self.cli_path is None:
            self._installed = False
            self._error = "claude not found"
            return None
        options = self._options(route, system, output_format, max_turns)
        self.sandbox.mkdir(parents=True, exist_ok=True)
        session = _Session()
        async with self._slots:
            try:
                async for message in self._query(prompt=prompt, options=options):
                    self._take(message, session)
            except CLINotFoundError as e:
                session.cli_missing = True
                session.exception = f"{type(e).__name__}: {e}"
            except (ClaudeSDKError, OSError) as e:
                # The SDK raises ResultError after yielding an error result (spike S1).
                session.exception = f"{type(e).__name__}: {e}"
        return session

    def _take(self, message: Message, session: _Session) -> None:
        match message:
            case SystemMessage(subtype="init"):
                session.resolved_model = message.data.get("model")
                self._installed = True
            case SystemMessage(subtype="model_fallback"):
                session.fallback_model = message.data.get("fallback_model") or "unknown"
            case RateLimitEvent():
                self._rate_limit = RateLimit.from_info(message.rate_limit_info)
                session.rejected = message.rate_limit_info.status == "rejected"
            case AssistantMessage():
                if message.error is not None:
                    session.errors.append(message.error)
                elif message.model != SYNTHETIC_MODEL:
                    session.answered_model = message.model
            case ResultMessage():
                session.result = message
            case _:
                pass

    def _failure(self, session: _Session) -> _Verdict | None:
        """The verdict on a spawn that got no usable answer for a reason other than its output;
        None when the output decides. Updates the status on the way.

        A result that isn't an error always goes to the output: its text is the model's answer,
        which may mention anything, and a rejected rate-limit event can arrive in a session that
        still answered."""
        result = session.result
        if result is not None and not result.is_error:
            self._logged_in = True
            self._error = None
            return None
        if session.cli_missing:
            self._installed = False
            return self._fail("error", session.exception)
        text = (result.result or "") if result else ""
        if "authentication_failed" in session.errors or "not logged in" in text.lower():
            self._logged_in = False
            return self._fail("error", f"not logged in: {text or session.exception}")
        status = result.api_error_status if result else None
        if session.rejected or "rate_limit" in session.errors or status in LIMIT_STATUSES:
            if "rate_limit" in session.errors and not session.rejected:
                self._rate_limit = RateLimit("rejected", None, None)
            return self._fail("limited", text or session.exception or f"HTTP {status}")
        if result is None:
            return self._fail("error", session.exception or "the session ended without a result")
        self._logged_in = True
        if not result.subtype.startswith("error_max"):
            detail = "; ".join(result.errors or []) or text or session.exception
            return self._fail("error", f"{result.subtype}: {detail}")
        self._error = None
        return None

    def _fail(self, outcome: Outcome, error: str | None) -> _Verdict:
        self._error = error
        return _Verdict(outcome, error)

    async def _record_hit(self, task: str, prompt_version: str, key: str, route: Route) -> None:
        """A cache hit's row: the job's log counts it, and nothing serves it again."""
        scope = current_scope()
        call = LlmCall(
            task=task,
            kind="structured",
            prompt_version=prompt_version,
            cache_key=key,
            model=route.model,
            effort=route.effort,
            attempt=1,
            outcome="ok",
            cached=True,
            job_id=scope.job_id,
            pack=scope.pack,
            table_name=scope.table_name,
        )
        await asyncio.to_thread(self.calls.record, call)

    async def _record(
        self,
        task: str,
        kind: Kind,
        prompt_version: str,
        key: str,
        route: Route,
        attempt: int,
        session: _Session,
        verdict: _Verdict,
    ) -> None:
        result = session.result
        scope = current_scope()
        call = LlmCall(
            task=task,
            kind=kind,
            prompt_version=prompt_version,
            cache_key=key,
            model=route.model,
            effort=route.effort,
            attempt=attempt,
            outcome=verdict.outcome,
            output=verdict.output,
            error=verdict.error,
            resolved_model=session.resolved_model,
            answered_model=session.answered_model or session.fallback_model,
            fallback=session.fallback_model is not None,
            usage=cast(dict[str, Any], result.model_usage) if result else None,
            cost_usd=result.total_cost_usd if result else None,
            duration_ms=result.duration_ms if result else None,
            num_turns=result.num_turns if result else None,
            session_id=result.session_id if result else None,
            job_id=scope.job_id,
            pack=scope.pack,
            table_name=scope.table_name,
        )
        await asyncio.to_thread(self.calls.record, call)


def _validate(session: _Session, schema: type[BaseModel]) -> _Verdict:
    result = session.result
    assert result is not None  # _failure has passed
    raw = result.structured_output
    if result.is_error or raw is None:
        return _Verdict("invalid", f"structured_output is missing ({result.subtype})")
    try:
        return _Verdict("ok", output=schema.model_validate(raw).model_dump(mode="json"))
    except ValidationError as e:
        return _Verdict("invalid", str(e), output=raw)


def _text(session: _Session) -> _Verdict:
    result = session.result
    assert result is not None
    if result.is_error or not result.result:
        return _Verdict("invalid", f"no text ({result.subtype})")
    return _Verdict("ok", output=result.result)


async def _cli_version(cli: Path) -> str | None:
    """`claude --version`'s version number, or None if it doesn't run or say one."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(cli),
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **call_env()},
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), VERSION_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    match = re.match(r"\s*(\d+\.\d+\.\d+)", out.decode(errors="replace"))
    return match.group(1) if proc.returncode == 0 and match else None


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _timestamp(seconds: int | None) -> datetime | None:
    return None if seconds is None else datetime.fromtimestamp(seconds, UTC)
