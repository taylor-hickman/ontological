"""Helpers for the tests that run the engine as a process (serve_stub.py, or the real one)."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from ontological.engine import discovery

STUB = Path(__file__).with_name("serve_stub.py")
PROCS: list[subprocess.Popen[str]] = []


class Proc:
    """An engine process in the foreground. It prints one JSON line once it serves or attaches:
    {"pid", "port", "version", "started"}, or {"error"}."""

    def __init__(
        self,
        cmd: list[str],
        *,
        ready: bool = True,
        env: dict[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
            pass_fds=pass_fds,
        )
        PROCS.append(self.proc)
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.info: dict[str, Any] = {}
        if ready:
            self.wait_ready()

    @classmethod
    def stub(cls, app_data: Path, *args: str, ready: bool = True) -> Proc:
        return cls([sys.executable, str(STUB), "--app-data", str(app_data), *args], ready=ready)

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)

    def wait_ready(self, timeout: float = 60) -> dict[str, Any]:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no ready line after {timeout}s") from None
        self.info = cast(dict[str, Any], json.loads(line))
        assert "error" not in self.info, self.info
        return self.info

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def wait_exit(self, timeout: float) -> int:
        return self.proc.wait(timeout)

    def term(self) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(30)


def http(app_data: Path, method: str, path: str, *, token: str | None = None) -> tuple[int, Any]:
    """A request to the engine engine.json names; (status, JSON body or None)."""
    info = discovery.read_engine_json(app_data)
    assert info is not None, "no engine.json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{info.port}{path}",
        method=method,
        headers={"Authorization": f"Bearer {info.token if token is None else token}"},
    )
    try:
        with discovery.LOCAL.open(req, timeout=10) as r:
            body = r.read()
            return r.status, json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        return e.code, None


def store_log(app_data: Path) -> list[str]:
    path = app_data / "store.log"
    return path.read_text().split() if path.exists() else []


def wait_until(cond: Any, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def kill_engines(app_data: Path) -> None:
    for p in PROCS:
        if p.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
    PROCS.clear()
    info = discovery.read_engine_json(app_data)  # a headless engine isn't our child
    if info is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(info.pid, signal.SIGKILL)


@pytest.fixture
def app_data() -> Iterator[Path]:
    """<appData>/Ontological, with a space in the path as `Application Support` has, under a
    short root: the real store's socket path must fit in 103 bytes."""
    root = Path(tempfile.mkdtemp(prefix="eng-", dir="/tmp"))
    path = root / "App Support" / "Ontological"
    try:
        yield path
    finally:
        kill_engines(path)
        shutil.rmtree(root, ignore_errors=True)
