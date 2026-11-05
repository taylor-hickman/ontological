"""Stand-ins the vendored-DAL tests share: an embedder and a keychain."""

from __future__ import annotations

import hashlib
from typing import ClassVar

from keyring.backend import KeyringBackend
from langchain_core.embeddings import Embeddings


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
