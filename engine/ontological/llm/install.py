"""What onboarding shows about the user's Claude Code (ARCHITECTURE.md §3, §6.5; DESIGN.md §5.1).

`detect` finds `claude`, reads its version, asks it whether the user is logged in, and names the
auth variables set in the engine's environment, which every call blanks (`env.py`). None of that
spends quota: `claude auth status --json` reads the local login and makes no request. It reads
`loggedIn` from the answer and nothing else, since the answer also names the account.

Ontological never runs the login flow. When `claude` says the user isn't logged in, the UI tells
them to open a terminal, run `claude` and type `/login`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from ontological.llm.env import AUTH_VARIABLES, call_env
from ontological.llm.gateway import (
    MIN_CLAUDE_VERSION,
    PING_SYSTEM,
    PING_USER,
    ClaudeGateway,
    cli_version,
    resolve_cli,
    version_tuple,
)

AUTH_STATUS_TIMEOUT_S = 10

InstallState = Literal["ready", "not_installed", "outdated", "needs_login", "unknown"]


@dataclass(frozen=True)
class ClaudeInstall:
    state: InstallState
    cli_path: Path | None
    version: str | None
    min_version: str
    logged_in: bool | None  # None when `claude auth status` gave no answer
    ignored_variables: list[str]  # auth variables set in the engine's environment


def ignored_variables(environ: Mapping[str, str] = os.environ) -> list[str]:
    """The auth variables set to something, which Ontological blanks for every call (§3 rule 3)."""
    return [name for name in AUTH_VARIABLES if environ.get(name)]


async def detect(
    cli_path: str | Path | None = None, *, environ: Mapping[str, str] = os.environ
) -> ClaudeInstall:
    """`cli_path` is the settings override; without it, `claude` on PATH."""
    cli = resolve_cli(cli_path)
    version = None if cli is None else await cli_version(cli)
    logged_in = None if version is None or cli is None else await logged_in_status(cli)
    state: InstallState
    if version is None:
        state = "not_installed"
    elif version_tuple(version) < MIN_CLAUDE_VERSION:
        state = "outdated"
    elif logged_in is False:
        state = "needs_login"
    elif logged_in:
        state = "ready"
    else:
        state = "unknown"
    return ClaudeInstall(
        state=state,
        cli_path=cli,
        version=version,
        min_version=".".join(str(part) for part in MIN_CLAUDE_VERSION),
        logged_in=logged_in,
        ignored_variables=ignored_variables(environ),
    )


async def logged_in_status(cli: Path) -> bool | None:
    """`loggedIn` from `claude auth status --json`, run with the auth variables blanked so it
    reports the login a call would use. None if it doesn't run or answer in JSON."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(cli),
            "auth",
            "status",
            "--json",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **call_env()},
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), AUTH_STATUS_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    # Read the JSON whatever the exit code: the answer, not the code, says logged in or not.
    try:
        answer = json.loads(out.decode(errors="replace"))
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    value = cast(dict[str, Any], answer).get("loggedIn")
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class PingResult:
    """The onboarding test call: a `ping` round trip on Haiku (DESIGN.md §5.1)."""

    ok: bool
    seconds: float | None  # how long the round trip took, spawn included
    state: str  # the gateway's state after the call: ready, needs_login, limited, ...
    error: str | None


class Claude(Protocol):
    """What the API's Claude routes call."""

    async def detect(self) -> ClaudeInstall: ...
    async def test_call(self) -> PingResult: ...


class ClaudeCheck:
    """`detect` against the user's `claude`, and the test call through a gateway the engine
    makes on first use (it logs the call in ops.llm_call, so it needs the store)."""

    def __init__(
        self,
        gateway: Callable[[], ClaudeGateway],
        *,
        cli_path: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._make_gateway = gateway
        self._gateway: ClaudeGateway | None = None
        self._cli_path = cli_path
        self._clock = clock

    async def detect(self) -> ClaudeInstall:
        return await detect(self._cli_path)

    async def test_call(self) -> PingResult:
        if self._gateway is None:
            self._gateway = self._make_gateway()
        started = self._clock()
        reply = await self._gateway.text("ping", PING_SYSTEM, PING_USER)
        seconds = round(self._clock() - started, 1)
        status = self._gateway.status
        if reply is None:
            return PingResult(ok=False, seconds=None, state=status.state, error=status.error)
        return PingResult(ok=True, seconds=seconds, state=status.state, error=None)
