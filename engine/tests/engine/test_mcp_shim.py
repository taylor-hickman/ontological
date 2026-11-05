"""The ontological-mcp shim (ARCHITECTURE.md §4.4, §9.2), run as mcp_stub.py: the real shim,
starting engines as serve_stub.py, the real engine with a fake store.

Leases show in the engine's log, logs/engine.log, since the shim starts engines headless: the
engine logs `lease <id> taken by ontological-mcp` and `lease <id> released`.
"""

from __future__ import annotations

import fcntl
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from engine_proc import PROCS, STUB, app_data, store_log, wait_until

from ontological.engine import discovery
from ontological.mcp.link import EngineUnavailable, hold_engine

__all__ = ["app_data"]  # the fixture

SHIM = Path(__file__).with_name("mcp_stub.py")
FAST = ("--idle-timeout", "1", "--lease-renew", "0.25")
PROTOCOL = "2025-06-18"  # what Claude Code sends


class Shim:
    """ontological-mcp as an MCP client sees it: JSON-RPC lines on its stdin and stdout."""

    def __init__(self, app_data: Path, *args: str) -> None:
        app_data.mkdir(parents=True, exist_ok=True)
        self.stderr = app_data.parent / "shim.err"
        with open(self.stderr, "ab") as err:
            self.proc = subprocess.Popen(
                [sys.executable, str(SHIM), "--app-data", str(app_data), *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                start_new_session=True,
            )
        PROCS.append(self.proc)
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.proc.stdin.flush()

    def receive(self, timeout: float) -> dict[str, Any]:
        try:
            return cast(dict[str, Any], json.loads(self.lines.get(timeout=timeout)))
        except queue.Empty:
            raise AssertionError(f"no reply after {timeout}s; see {self.stderr}") from None

    def initialize(self, timeout: float = 30) -> dict[str, Any]:
        self.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        reply = self.receive(timeout)
        self.send({"method": "notifications/initialized"})
        return reply

    def close(self, timeout: float = 15) -> int:
        """Close its stdin, as an MCP client does when it is done, and wait for it to exit."""
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        return self.proc.wait(timeout)


def engine_log(app_data: Path) -> str:
    path = app_data / "logs" / "engine.log"
    return path.read_text() if path.exists() else ""


def leases_taken(app_data: Path) -> int:
    return engine_log(app_data).count("taken by ontological-mcp")


def leases_released(app_data: Path) -> int:
    return engine_log(app_data).count(" released")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def live_engine(app_data: Path) -> discovery.EngineInfo:
    info = discovery.find_engine(app_data)
    assert info is not None, "no live engine"
    return info


def headless(app_data: Path, *args: str) -> int:
    """Start an engine as the shim would, and return its pid."""
    p = subprocess.run(
        [sys.executable, str(STUB), "--app-data", str(app_data), *args, "--headless"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert p.returncode == 0, p.stderr
    return int(json.loads(p.stdout)["pid"])


def test_initialize_is_answered_before_the_engine_serves(app_data: Path) -> None:
    app_data.mkdir(parents=True)
    (app_data / "slow-start").write_text("5")
    shim = Shim(app_data, *FAST)
    reply = shim.initialize()
    assert reply["id"] == 1
    assert reply["result"]["serverInfo"]["name"] == "ontological"
    assert reply["result"]["protocolVersion"] == PROTOCOL
    assert discovery.read_engine_json(app_data) is None, "the engine is still starting"
    wait_until(lambda: leases_taken(app_data) == 1, 30, "the shim's lease")


def test_with_no_engine_the_shim_starts_one_and_on_close_lets_it_idle_out(
    app_data: Path,
) -> None:
    shim = Shim(app_data, *FAST)
    shim.initialize()
    wait_until(lambda: leases_taken(app_data) == 1, 30, "the shim's lease")
    engine = live_engine(app_data)
    time.sleep(2.5)  # past the 1 s idle timeout: the lease keeps it up
    assert alive(engine.pid)
    assert store_log(app_data) == ["start"]

    assert shim.close() == 0
    assert leases_released(app_data) == 1
    wait_until(lambda: not alive(engine.pid), 15, "the engine to idle out")
    assert store_log(app_data) == ["start", "quit-dump", "stop"]


def test_the_shim_attaches_to_a_running_engine(app_data: Path) -> None:
    pid = headless(app_data, "--idle-timeout", "30")
    shim = Shim(app_data, *FAST)
    shim.initialize()
    wait_until(lambda: leases_taken(app_data) == 1, 30, "the shim's lease")
    assert live_engine(app_data).pid == pid
    assert store_log(app_data) == ["start"]
    assert shim.close() == 0
    assert leases_released(app_data) == 1


def test_sigterm_releases_the_lease(app_data: Path) -> None:
    shim = Shim(app_data, *FAST)
    shim.initialize()
    wait_until(lambda: leases_taken(app_data) == 1, 30, "the shim's lease")
    shim.proc.send_signal(signal.SIGTERM)
    assert shim.proc.wait(15) == 0
    assert leases_released(app_data) == 1


def test_a_lapsed_lease_is_taken_again(app_data: Path) -> None:
    # A lease lapses after three missed renewals (0.75 s here), as when the machine sleeps.
    shim = Shim(app_data, "--idle-timeout", "30", "--lease-renew", "0.25")
    shim.initialize()
    wait_until(lambda: leases_taken(app_data) == 1, 30, "the shim's lease")
    shim.proc.send_signal(signal.SIGSTOP)
    time.sleep(1.5)
    shim.proc.send_signal(signal.SIGCONT)
    wait_until(lambda: leases_taken(app_data) == 2, 10, "a new lease")
    assert store_log(app_data) == ["start"]


def test_an_engine_that_dies_is_started_again(app_data: Path) -> None:
    shim = Shim(app_data, *FAST)
    shim.initialize()
    wait_until(lambda: leases_taken(app_data) == 1, 30, "the shim's lease")
    first = live_engine(app_data)
    os.kill(first.pid, signal.SIGKILL)
    wait_until(lambda: leases_taken(app_data) == 2, 30, "a lease on a new engine")
    assert live_engine(app_data).pid != first.pid


def test_a_closing_engine_sends_the_shim_to_start_another(app_data: Path) -> None:
    # The old engine idles out but can't stop while the test holds engine.lock, so it answers
    # /health and refuses leases with 503. The shim starts a new engine, which waits for the old
    # one to stop.
    old = headless(app_data, *FAST)
    with open(app_data / "engine.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        time.sleep(2)  # the old engine is idle, and closing
        assert live_engine(app_data).pid == old
        shim = Shim(app_data, *FAST)
        shim.initialize()
        wait_until(lambda: "closing" in shim.stderr.read_text(), 30, "the shim to get a 503")
        fcntl.flock(lock, fcntl.LOCK_UN)
    wait_until(lambda: leases_taken(app_data) == 1, 30, "a lease on a new engine")
    assert not alive(old)
    new = live_engine(app_data)
    assert new.pid != old
    time.sleep(2)  # past the new engine's idle timeout: the shim's lease keeps it up
    assert alive(new.pid)


# --- the link itself, which tool calls wait on --------------------------------------------------


def start_cmd(app_data: Path) -> list[str]:
    return [sys.executable, str(STUB), "--app-data", str(app_data), *FAST, "--headless"]


def test_engine_waits_for_the_engine_to_serve(app_data: Path) -> None:
    app_data.mkdir(parents=True)
    (app_data / "slow-start").write_text("1")

    async def go() -> None:
        async with hold_engine(app_data, start_cmd(app_data)) as link:
            info = await link.engine()
            assert info == discovery.read_engine_json(app_data)
            assert leases_taken(app_data) == 1
        assert leases_released(app_data) == 1

    anyio.run(go)


def test_an_engine_that_cant_start_fails_the_call_and_the_next_call_tries_again(
    app_data: Path,
) -> None:
    app_data.mkdir(parents=True)
    (app_data / "store.log").mkdir()  # FakeStore can't append to a directory

    async def go() -> None:
        async with hold_engine(app_data, start_cmd(app_data)) as link:
            with pytest.raises(EngineUnavailable, match="exited with 1"):
                await link.engine()
            shutil.rmtree(app_data / "store.log")
            info = await link.engine()
            assert info == discovery.read_engine_json(app_data)

    anyio.run(go)
