"""Stand-ins tests share: an embedder, a keychain, and a Claude gateway for compile."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from keyring.backend import KeyringBackend
from langchain_core.embeddings import Embeddings
from pydantic import BaseModel


class HashEmbeddings(Embeddings):
    """Deterministic unit vectors from a text's hash, `dim` wide. The local embedder is M1.5."""

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        raw = [b - 127.5 for b in digest[: self.dim]]
        norm = sum(v * v for v in raw) ** 0.5
        return [v / norm for v in raw]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class MemoryKeyring(KeyringBackend):
    """A keychain in a dict, so no test touches the real one."""

    priority: ClassVar[float] = 1  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.items[(service, username)]


@dataclass(frozen=True)
class StructuredCall:
    task: str
    system: str
    user: str
    schema: type[BaseModel]
    prompt_version: str


Answer = Callable[[str, str], dict[str, Any] | None]


@dataclass
class ScriptedGateway:
    """`ClaudeGateway.structured()` for compile tests: `answer(task, user)` gives the model's
    JSON, or None for a failed call. Records every call, and the most that were in flight at
    once."""

    answer: Answer
    calls: list[StructuredCall] = field(default_factory=list[StructuredCall])
    in_flight: int = 0
    peak: int = 0

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
        self.calls.append(StructuredCall(task, system, user, schema, prompt_version))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0)  # let anything else that could run, run
            output = self.answer(task, user)
        finally:
            self.in_flight -= 1
        return None if output is None else schema.model_validate(output)

    def tasks(self) -> list[str]:
        return [c.task for c in self.calls]
