"""What compile needs from the Claude gateway (ARCHITECTURE.md §6.1), and the line between a bad
answer and no answer.

`structured()` returns None for both. Upstream's deterministic fallbacks are for a model that
answered badly. When Claude didn't answer at all (a rate limit, an overload, a logged-out or
missing `claude`, a failed spawn), a fallback would be committed as if it were the table's
ontology, and the table would leave the work list for good. So `ask` raises `ClaudeUnavailable`
instead, and compile stops before writing the table. Tests answer without spawning `claude`.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from ontological.llm.calls import Outcome
from ontological.llm.gateway import GatewayStatus


class Structured(Protocol):
    @property
    def last_outcome(self) -> Outcome | None: ...

    @property
    def status(self) -> GatewayStatus: ...

    async def structured[M: BaseModel](
        self,
        task: str,
        system: str,
        user: str,
        schema: type[M],
        cache_key_extra: str = "",
        *,
        prompt_version: str = "",
    ) -> M | None: ...


class ClaudeUnavailable(Exception):
    """Claude didn't answer a compile call. Nothing for the current table is written."""

    def __init__(self, task: str, outcome: Outcome | None, status: GatewayStatus) -> None:
        self.task = task
        self.outcome = outcome
        self.status = status
        detail = status.error or status.state
        if status.limited_until is not None:
            detail += f", until {status.limited_until.isoformat()}"
        super().__init__(f"{task}: Claude didn't answer ({outcome}; {detail})")


async def ask[M: BaseModel](
    gateway: Structured, task: str, system: str, user: str, schema: type[M], prompt_version: str
) -> M | None:
    """The model's answer; None when it answered badly, so the caller falls back. Raises
    `ClaudeUnavailable` when it didn't answer."""
    result = await gateway.structured(task, system, user, schema, prompt_version=prompt_version)
    if result is None and gateway.last_outcome != "invalid":
        raise ClaudeUnavailable(task, gateway.last_outcome, gateway.status)
    return result
