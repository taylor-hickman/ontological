"""The shim's hold on an engine (ARCHITECTURE.md §4.4, §9.2).

`hold_engine` attaches in the background to the engine engine.json names, or starts one with
`--headless` if none answers, and takes a lease on it. It renews the lease every
`renew_every_s`, takes a new one if the engine has let it lapse (404), and attaches again if the
engine stops answering. It releases the lease on exit, and the engine idles out once no client
holds one.

A closing engine refuses a lease with 503, but it answers /health and engine.json names it until
it has taken engine.lock to stop. So the shim waits until engine.json no longer names it, or it
has died, and attaches again, which starts a new engine. The shim never takes engine.lock
itself; the engine takes it to settle which of two starting engines serves.

Tool calls go through `EngineLink.call()`, which waits on `EngineLink.engine()`, so a call made
while the engine starts waits for it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio
import httpx

from ontological.engine import discovery

log = logging.getLogger("ontological.mcp")

CLIENT = "ontological-mcp"
ATTACH_ATTEMPTS = 5  # engines in a row that vanish or don't answer, before giving up
ATTEMPT_PAUSE_S = 0.2
CLOSING_WAIT_S = 300.0  # a closing engine waits for engine.lock, which a starting one holds
CLOSING_POLL_S = 0.1
REQUEST_TIMEOUT_S = 10.0
RELEASE_TIMEOUT_S = 2.0


class EngineUnavailable(Exception):
    """No engine could be started or attached to; the message says why."""


class _Closing(Exception):
    """The engine refused a lease with 503: it is stopping."""


@dataclass(frozen=True)
class _Lease:
    id: str
    renew_every_s: float


class EngineLink:
    def __init__(self, app_data: Path, start: Sequence[str], *, client: str = CLIENT) -> None:
        """`start` is the whole command that starts an engine, `--headless` included. `client`
        names the holder in the engine's log."""
        self.app_data = app_data
        self.start = list(start)
        self.client = client
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, trust_env=False)  # no proxy
        self._engine: discovery.EngineInfo | None = None
        self._lease: _Lease | None = None
        self._error: EngineUnavailable | None = None
        self._changed = anyio.Event()
        self._retry = anyio.Event()

    async def engine(self) -> discovery.EngineInfo:
        """The engine this shim holds a lease on, once it has one. If the last attempt to attach
        failed, this tries again, and raises EngineUnavailable if that fails too."""
        if self._engine is None and self._error is not None:
            self._error = None
            self._retry.set()
        while self._engine is None:
            if self._error is not None:
                raise self._error
            await self._changed.wait()
        return self._engine

    async def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float,
    ) -> httpx.Response:
        """One request to the engine's API, once there is an engine. Raises EngineUnavailable
        when there is none, or it doesn't answer within `timeout` seconds."""
        info = await self.engine()
        try:
            return await self._http.request(
                method,
                f"http://127.0.0.1:{info.port}{path}",
                headers={"Authorization": f"Bearer {info.token}"},
                json=body,
                params=params,
                timeout=timeout,
            )
        except httpx.TimeoutException as e:
            raise EngineUnavailable(f"the engine didn't answer in {timeout:g}s") from e
        except httpx.TransportError as e:
            raise EngineUnavailable(f"the engine didn't answer: {e}") from e

    def _notify(self) -> None:
        self._changed.set()
        self._changed = anyio.Event()

    async def run(self) -> None:
        """Attach, hold the lease, and attach again whenever the engine is lost. Until
        cancelled."""
        while True:
            try:
                self._engine, self._lease = await self._attach()
            except EngineUnavailable as e:
                log.error("no engine: %s", e)
                self._retry = anyio.Event()
                self._error = e
                self._notify()
                await self._retry.wait()  # until a tool call asks for the engine
                continue
            log.info("attached to engine %d on port %d", self._engine.pid, self._engine.port)
            self._notify()
            await self._hold()
            self._engine = self._lease = None

    async def release(self) -> None:
        """Release the lease, if there is one, and close the HTTP client."""
        engine, lease = self._engine, self._lease
        self._engine = self._lease = None
        with anyio.move_on_after(RELEASE_TIMEOUT_S, shield=True):
            if engine is not None and lease is not None:
                await self._call(engine, "DELETE", f"/leases/{lease.id}")
            await self._http.aclose()

    async def _attach(self) -> tuple[discovery.EngineInfo, _Lease]:
        """Waiting out a closing engine doesn't count as a failed attempt."""
        failures = 0
        while True:
            info = await anyio.to_thread.run_sync(discovery.find_engine, self.app_data)
            if info is None:
                info = await self._start()
            if info is not None:
                try:
                    if (lease := await self._take_lease(info)) is not None:
                        return info, lease
                except _Closing:
                    await self._outlast(info)
                    continue
            failures += 1
            if failures == ATTACH_ATTEMPTS:
                raise EngineUnavailable(f"no engine took a lease after {failures} attempts")
            await anyio.sleep(ATTEMPT_PAUSE_S)

    async def _outlast(self, info: discovery.EngineInfo) -> None:
        """Wait until engine.json no longer names this closing engine, or it has died."""
        log.info("engine %d is closing: waiting for it to stop", info.pid)
        with anyio.move_on_after(CLOSING_WAIT_S):
            while discovery.pid_alive(info.pid):
                named = discovery.read_engine_json(self.app_data)
                if named is None or named.pid != info.pid:
                    return
                await anyio.sleep(CLOSING_POLL_S)
            return
        raise EngineUnavailable(f"engine {info.pid} didn't stop in {CLOSING_WAIT_S:g}s")

    async def _start(self) -> discovery.EngineInfo | None:
        """Start an engine and return what engine.json says once it serves, or None if it has
        already gone. The launcher prints one JSON line and exits once an engine serves."""
        log.info("starting an engine")
        try:
            done = await anyio.run_process(self.start, stdin=subprocess.DEVNULL, check=False)
        except OSError as e:
            raise EngineUnavailable(f"couldn't run {self.start[0]}: {e}") from e
        out = done.stdout.decode(errors="replace").strip().splitlines()
        try:
            said = cast(dict[str, Any], json.loads(out[-1]))
        except (IndexError, ValueError):
            said = {}
        if done.returncode != 0 or "pid" not in said:
            why = said.get("error") or done.stderr.decode(errors="replace").strip()
            raise EngineUnavailable(why or f"the engine launcher exited with {done.returncode}")
        info = discovery.read_engine_json(self.app_data)
        return info if info is not None and info.pid == said["pid"] else None

    async def _take_lease(self, info: discovery.EngineInfo) -> _Lease | None:
        """A new lease, or None if the engine doesn't answer. Raises _Closing on a 503."""
        status, body = await self._call(info, "POST", "/leases", {"client": self.client})
        if status == 201 and body is not None:
            return _Lease(str(body["lease"]), float(body["renew_every_s"]))
        if status == 503:
            raise _Closing
        log.warning("engine %d refused a lease: %s", info.pid, status or "no answer")
        return None

    async def _hold(self) -> None:
        """Renew the lease until the engine is lost."""
        assert self._engine is not None and self._lease is not None
        engine = self._engine
        while True:
            await anyio.sleep(self._lease.renew_every_s)
            status, _ = await self._call(engine, "PUT", f"/leases/{self._lease.id}")
            if status == 200:
                continue
            if status == 404:  # lapsed, as when the machine slept
                log.info("lease lapsed: taking a new one")
                with contextlib.suppress(_Closing):  # attaching again waits it out
                    if (lease := await self._take_lease(engine)) is not None:
                        self._lease = lease
                        continue
            log.warning("lost engine %d (%s): attaching again", engine.pid, status or "no answer")
            return

    async def _call(
        self,
        info: discovery.EngineInfo,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int | None, dict[str, Any] | None]:
        """(status, JSON body), or (None, None) if the engine didn't answer."""
        try:
            r = await self._http.request(
                method,
                f"http://127.0.0.1:{info.port}{path}",
                headers={"Authorization": f"Bearer {info.token}"},
                json=body,
            )
        except httpx.TransportError:
            return None, None
        try:
            data: Any = r.json() if r.content else None
        except ValueError:
            data = None
        return r.status_code, cast(dict[str, Any], data) if isinstance(data, dict) else None


@contextlib.asynccontextmanager
async def hold_engine(
    app_data: Path, start: Sequence[str], *, client: str = CLIENT
) -> AsyncGenerator[EngineLink]:
    """An EngineLink attaching in the background; its lease is released on exit."""
    link = EngineLink(app_data, start, client=client)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(link.run)
            try:
                yield link
            finally:
                tg.cancel_scope.cancel()
    finally:
        await link.release()
