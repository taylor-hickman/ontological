"""What compile needs from the Claude gateway: `ClaudeGateway.structured()` (ARCHITECTURE.md
§6.1). Tests answer it without spawning `claude`."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class Structured(Protocol):
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
