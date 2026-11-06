"""The jobs this engine is running now (ARCHITECTURE.md §4.4).

A job counts from when it starts until it returns, so a paused one counts too, and a compile
waiting for a usage reset keeps the engine up. The count lives in memory, not in ops.job: a job
an earlier engine was running when it crashed is still `running` there, and must not keep this
engine up. A database runs one job of a kind at a time, so two compiles can't both resume it.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager


class JobBusy(Exception):
    """That job is already running in this engine."""


class ActiveJobs:
    def __init__(self) -> None:
        self._running: set[tuple[str, str]] = set()

    def count(self) -> int:
        return len(self._running)

    @contextmanager
    def track(self, kind: str, database_name: str) -> Generator[None]:
        key = (kind, database_name)
        if key in self._running:
            raise JobBusy(f"a {kind} of {database_name} is already running")
        self._running.add(key)
        try:
            yield
        finally:
            self._running.discard(key)
