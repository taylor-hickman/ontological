"""Leases and idle shutdown (ARCHITECTURE.md §4.4).

Every client of the engine (the Electron app, each `ontological-mcp` shim, the eval runner) holds
a lease and renews it every `renew_every_s`. A lease that misses three renewals expires, which
covers a client that crashed. The engine may exit once it has had no live lease, no request in
flight and no running job for `idle_timeout_s`.

The engine's event loop is the only caller, so nothing here locks.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

IDLE_TIMEOUT_S = 600.0
RENEW_EVERY_S = 30.0
MISSED_RENEWALS = 3


class Closing(Exception):
    """The engine has decided to exit; a client that wants a lease starts a new engine."""


@dataclass(frozen=True)
class Lease:
    id: str
    renew_every_s: float
    expires_after_s: float


class Lifetime:
    def __init__(
        self,
        *,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        renew_every_s: float = RENEW_EVERY_S,
        clock: Callable[[], float] = time.monotonic,
        jobs_running: Callable[[], int] = lambda: 0,
    ) -> None:
        """`jobs_running` counts running jobs, paused ones included, so a compile waiting for a
        usage reset keeps the engine up."""
        self.idle_timeout_s = idle_timeout_s
        self.renew_every_s = renew_every_s
        self.expires_after_s = renew_every_s * MISSED_RENEWALS
        self.closing = False
        self._clock = clock
        self._jobs_running = jobs_running
        self._expiry: dict[str, float] = {}  # lease id -> when it expires
        self._in_flight = 0
        self._idle_since = clock()

    def _lease(self, lease_id: str) -> Lease:
        return Lease(lease_id, self.renew_every_s, self.expires_after_s)

    def _sweep(self) -> None:
        """Drop expired leases. Idle time counts from when a lease expired, not from when it was
        noticed."""
        now = self._clock()
        for lease_id, expires in list(self._expiry.items()):
            if expires <= now:
                del self._expiry[lease_id]
                self._idle_since = max(self._idle_since, expires)

    def take(self, client: str = "") -> Lease:
        """A new lease. `client` names who holds it, for the log."""
        if self.closing:
            raise Closing
        self._sweep()
        lease_id = secrets.token_urlsafe(16)
        self._expiry[lease_id] = self._clock() + self.expires_after_s
        return self._lease(lease_id)

    def renew(self, lease_id: str) -> Lease | None:
        """None if the lease has expired or was never issued: the client takes a new one."""
        self._sweep()
        if lease_id not in self._expiry:
            return None
        self._expiry[lease_id] = self._clock() + self.expires_after_s
        return self._lease(lease_id)

    def release(self, lease_id: str) -> bool:
        self._sweep()
        if self._expiry.pop(lease_id, None) is None:
            return False
        self._idle_since = max(self._idle_since, self._clock())
        return True

    def live_leases(self) -> int:
        self._sweep()
        return len(self._expiry)

    def request_started(self) -> None:
        self._in_flight += 1

    def request_finished(self) -> None:
        self._in_flight -= 1
        self._idle_since = max(self._idle_since, self._clock())

    def close(self) -> None:
        """Take no new leases: the engine is stopping (a signal, say)."""
        self.closing = True

    def should_exit(self) -> bool:
        """Whether the engine has been idle for the timeout. Once this returns True the engine
        is closing and takes no new leases."""
        if self.closing:
            return True
        self._sweep()
        now = self._clock()
        if self._expiry or self._in_flight or self._jobs_running():
            self._idle_since = now
            return False
        if now - self._idle_since >= self.idle_timeout_s:
            self.closing = True
        return self.closing
