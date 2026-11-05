"""The engine process (ARCHITECTURE.md §4.3, §4.4), run as serve_stub.py: the real engine with a
fake store that logs what it was asked to do."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from engine_proc import STUB, Proc, app_data, http, store_log, wait_until

import ontological
from ontological.engine import discovery, main
from ontological.engine.discovery import EngineInfo

__all__ = ["app_data"]  # the fixture


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


FAST = ("--idle-timeout", "1", "--lease-renew", "0.25")


def test_engine_json_names_the_serving_engine(app_data: Path) -> None:
    e = Proc.stub(app_data)
    assert e.info == {
        "pid": e.pid,
        "port": e.info["port"],
        "version": ontological.__version__,
        "started": True,
    }
    path = app_data / "engine.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert set(data) == {"pid", "port", "token", "version"}
    assert data["pid"] == e.pid
    assert data["port"] == e.info["port"]
    assert data["version"] == ontological.__version__
    assert len(base64.urlsafe_b64decode(data["token"] + "=")) == 32
    assert http(app_data, "GET", "/health") == (
        200,
        {"pid": e.pid, "version": ontological.__version__},
    )
    assert http(app_data, "GET", "/health", token="wrong")[0] == 401
    assert store_log(app_data) == ["start"]


def test_each_engine_gets_a_new_token(app_data: Path) -> None:
    e = Proc.stub(app_data)
    first = discovery.read_engine_json(app_data)
    assert e.term() == 0
    Proc.stub(app_data)
    second = discovery.read_engine_json(app_data)
    assert first is not None and second is not None
    assert first.token != second.token
    assert first.pid != second.pid


def test_sigterm_writes_the_quit_dump_stops_the_store_and_removes_engine_json(
    app_data: Path,
) -> None:
    e = Proc.stub(app_data)
    assert e.term() == 0
    assert store_log(app_data) == ["start", "quit-dump", "stop"]
    assert not (app_data / "engine.json").exists()


def test_an_engine_starting_while_another_stops_never_overlaps_its_store(
    app_data: Path,
) -> None:
    # Otherwise the new engine could adopt the Postgres the old one is about to stop. The test
    # holds engine.lock (a client reading engine.json) while the old engine is told to stop and
    # a new one starts, so both wait on it.
    old = Proc.stub(app_data)
    with open(app_data / "engine.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old.proc.send_signal(15)
        time.sleep(0.5)
        assert http(app_data, "GET", "/health")[0] == 200, "it serves until it has the lock"
        new = Proc.stub(app_data, ready=False)
        time.sleep(0.5)
        fcntl.flock(lock, fcntl.LOCK_UN)
    info = new.wait_ready()
    assert old.wait_exit(10) == 0
    if info["started"]:  # the old engine got the lock first, and stopped its store
        assert store_log(app_data) == ["start", "quit-dump", "stop", "start"]
    else:  # the new engine got it first, found the old one serving and left
        assert info["pid"] == old.pid
        assert store_log(app_data) == ["start", "quit-dump", "stop"]


def test_a_second_engine_attaches_to_the_first_instead_of_starting(app_data: Path) -> None:
    first = Proc.stub(app_data)
    before = (app_data / "engine.json").read_text()
    second = Proc.stub(app_data)
    assert second.info == {
        "pid": first.pid,
        "port": first.info["port"],
        "version": ontological.__version__,
        "started": False,
    }
    assert second.wait_exit(10) == 0
    assert first.alive()
    assert (app_data / "engine.json").read_text() == before
    assert store_log(app_data) == ["start"]


def test_engines_started_at_once_make_one_engine(app_data: Path) -> None:
    engines = [Proc.stub(app_data, ready=False) for _ in range(4)]
    infos = [e.wait_ready() for e in engines]
    assert sum(i["started"] for i in infos) == 1
    serving = next(e for e, i in zip(engines, infos, strict=True) if i["started"])
    assert {i["pid"] for i in infos} == {serving.pid}
    for e in engines:
        if e is not serving:
            assert e.wait_exit(10) == 0
    assert store_log(app_data) == ["start"]


class FakeProxy:
    """A proxy that records whoever connects to it and answers 502."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.requests: list[bytes] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                self.requests.append(conn.recv(65536))
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")

    def close(self) -> None:
        self.sock.close()


PROXY_VARS = ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")


def test_finding_the_engine_ignores_a_proxy(
    app_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A proxy must never see the token, and can't reach the engine anyway.
    e = Proc.stub(app_data)
    proxy = FakeProxy()
    try:
        for var in PROXY_VARS:
            monkeypatch.setenv(var, proxy.url)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        live = discovery.find_engine(app_data)
        assert live is not None and live.pid == e.pid
        assert proxy.requests == []
    finally:
        proxy.close()


def test_an_engine_started_behind_a_proxy_still_attaches(app_data: Path) -> None:
    first = Proc.stub(app_data)
    proxy = FakeProxy()
    env = {k: v for k, v in os.environ.items() if k.lower() != "no_proxy"}
    env.update(dict.fromkeys(PROXY_VARS, proxy.url))
    try:
        second = Proc([sys.executable, str(STUB), "--app-data", str(app_data)], env=env)
        assert second.info["started"] is False
        assert second.info["pid"] == first.pid
        assert second.wait_exit(10) == 0
        assert proxy.requests == []
        assert store_log(app_data) == ["start"]
    finally:
        proxy.close()


def test_a_stale_engine_json_is_replaced(app_data: Path) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    app_data.mkdir(parents=True)
    discovery.write_engine_json(app_data, EngineInfo(gone.pid, 1, "old", "0"))
    e = Proc.stub(app_data)
    assert e.info["started"]
    info = discovery.read_engine_json(app_data)
    assert info is not None and info.pid == e.pid and info.token != "old"


def test_the_engine_waits_for_engine_lock(app_data: Path) -> None:
    app_data.mkdir(parents=True)
    with open(app_data / "engine.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # a client reading engine.json, say
        e = Proc.stub(app_data, ready=False)
        time.sleep(1)
        assert e.alive()
        assert not (app_data / "engine.json").exists()
        assert store_log(app_data) == [], "the store starts only under the lock"
        fcntl.flock(lock, fcntl.LOCK_UN)
    assert e.wait_ready()["started"]


def test_an_idle_engine_shuts_down(app_data: Path) -> None:
    t0 = time.monotonic()
    e = Proc.stub(app_data, *FAST)
    assert e.wait_exit(15) == 0
    assert time.monotonic() - t0 >= 1
    assert store_log(app_data) == ["start", "quit-dump", "stop"]
    assert not (app_data / "engine.json").exists()


def test_a_renewed_lease_keeps_the_engine_up_and_a_lapsed_one_lets_it_go(
    app_data: Path,
) -> None:
    e = Proc.stub(app_data, *FAST)
    status, lease = http(app_data, "POST", "/leases")
    assert status == 201
    assert lease["renew_every_s"] == 0.25
    assert lease["expires_after_s"] == 0.75
    t_end = time.monotonic() + 3  # three idle timeouts
    while time.monotonic() < t_end:
        assert http(app_data, "PUT", f"/leases/{lease['lease']}")[0] == 200
        time.sleep(0.15)
    last = time.monotonic()
    # The lease lapses 0.75 s after the last renewal, and the engine idles out 1 s after that.
    time.sleep(1.5)
    assert e.alive()
    assert e.wait_exit(15) == 0
    assert time.monotonic() - last >= 1.7
    assert store_log(app_data) == ["start", "quit-dump", "stop"]


def test_releasing_the_last_lease_starts_the_idle_clock(app_data: Path) -> None:
    e = Proc.stub(app_data, "--idle-timeout", "1", "--lease-renew", "30")
    _, lease = http(app_data, "POST", "/leases")
    time.sleep(2)
    assert e.alive(), "a 30 s lease outlives two idle timeouts"
    assert http(app_data, "DELETE", f"/leases/{lease['lease']}")[0] == 204
    released = time.monotonic()
    assert e.wait_exit(15) == 0
    assert time.monotonic() - released >= 0.9


def test_headless_returns_once_the_engine_serves_and_the_engine_outlives_it(
    app_data: Path,
) -> None:
    def launch() -> tuple[subprocess.CompletedProcess[str], dict[str, int | bool | str]]:
        p = subprocess.run(
            [sys.executable, str(STUB), "--app-data", str(app_data), "--headless"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert p.returncode == 0, p.stderr
        return p, json.loads(p.stdout)

    _, out = launch()
    assert out["started"] is True
    pid = int(out["pid"])
    info = discovery.read_engine_json(app_data)
    assert info is not None and info.pid == pid and info.port == out["port"]
    # The launcher has exited; the engine runs on in its own session.
    assert os.getsid(pid) == pid
    assert http(app_data, "GET", "/health")[0] == 200
    assert (app_data / "logs" / "engine.log").exists()

    _, again = launch()
    assert again == {**out, "started": False}

    os.kill(pid, 15)
    wait_until(lambda: not alive(pid), 15, "the engine to stop")
    assert not (app_data / "engine.json").exists()
    assert store_log(app_data) == ["start", "quit-dump", "stop"]


def test_a_store_that_fails_to_start_is_reported_and_leaves_no_engine_json(
    app_data: Path,
) -> None:
    app_data.mkdir(parents=True)
    (app_data / "store.log").mkdir()  # FakeStore can't append to a directory
    e = Proc.stub(app_data, ready=False)
    line = json.loads(e.lines.get(timeout=30))
    assert "error" in line
    assert e.wait_exit(10) == 1
    assert not (app_data / "engine.json").exists()


def test_a_line_to_a_reader_that_has_gone_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Electron reads the engine's line and may quit while its engine runs on (ARCHITECTURE.md
    §4.4). A later line, such as an error, must not raise and skip stopping the store."""
    r, w = os.pipe()
    os.close(r)
    gone = os.fdopen(w, "w")
    monkeypatch.setattr(sys, "stdout", gone)
    main.emit(error="RuntimeError: boom")
    main.emit(error="and again")
    replaced = sys.stdout
    monkeypatch.undo()
    assert replaced is not gone
    replaced.close()
    with contextlib.suppress(BrokenPipeError):  # the first line is still in its buffer
        gone.close()
