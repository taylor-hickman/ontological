"""engine.json and engine.lock (ARCHITECTURE.md §4.3, §4.4), without a running engine."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

from ontological.engine import discovery
from ontological.engine.discovery import EngineInfo


def dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_engine_json_is_written_at_mode_0600(tmp_path: Path) -> None:
    info = EngineInfo(pid=123, port=4567, token="secret", version="1.2.3")
    old = os.umask(0)  # mode 0600 doesn't rely on the umask
    try:
        discovery.write_engine_json(tmp_path, info)
    finally:
        os.umask(old)
    path = tmp_path / "engine.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "pid": 123,
        "port": 4567,
        "token": "secret",
        "version": "1.2.3",
    }
    assert discovery.read_engine_json(tmp_path) == info
    assert [p.name for p in tmp_path.iterdir()] == ["engine.json"], "no temporary file is left"


def test_rewriting_engine_json_keeps_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "engine.json"
    path.write_text("{}")
    path.chmod(0o644)
    discovery.write_engine_json(tmp_path, EngineInfo(1, 2, "t", "v"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_missing_or_damaged_engine_json_reads_as_none(tmp_path: Path) -> None:
    assert discovery.read_engine_json(tmp_path) is None
    path = tmp_path / "engine.json"
    for text in [
        "",
        "{",
        "[]",
        '{"pid": 1}',
        '{"pid": "1", "port": 2, "token": "t", "version": "v"}',
    ]:
        path.write_text(text)
        assert discovery.read_engine_json(tmp_path) is None, text


def test_engine_json_naming_a_dead_pid_is_no_engine(tmp_path: Path) -> None:
    discovery.write_engine_json(tmp_path, EngineInfo(dead_pid(), 1, "t", "v"))
    assert discovery.find_engine(tmp_path) is None


def test_engine_json_naming_a_live_pid_that_does_not_answer_is_no_engine(tmp_path: Path) -> None:
    # After a crash and a reboot the pid can belong to anything; only the engine answers /health
    # with the token.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    discovery.write_engine_json(tmp_path, EngineInfo(os.getpid(), port, "t", "v"))
    assert discovery.find_engine(tmp_path) is None


def test_an_engine_removes_only_its_own_engine_json(tmp_path: Path) -> None:
    discovery.write_engine_json(tmp_path, EngineInfo(111, 1, "t", "v"))
    discovery.remove_engine_json(tmp_path, pid=222)
    assert (tmp_path / "engine.json").exists()
    discovery.remove_engine_json(tmp_path, pid=111)
    assert not (tmp_path / "engine.json").exists()
    discovery.remove_engine_json(tmp_path, pid=111)  # already gone: no error


def test_engine_lock_is_exclusive(tmp_path: Path) -> None:
    order: list[str] = []
    with discovery.engine_lock(tmp_path):
        assert (tmp_path / "engine.lock").exists()

        def other() -> None:
            with discovery.engine_lock(tmp_path):
                order.append("other")

        t = threading.Thread(target=other)
        t.start()
        time.sleep(0.3)
        order.append("first")
    t.join(5)
    assert order == ["first", "other"]


def test_engine_lock_is_an_flock_other_processes_see(tmp_path: Path) -> None:
    with discovery.engine_lock(tmp_path), open(tmp_path / "engine.lock") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise AssertionError("engine.lock was not held")
        except BlockingIOError:
            pass


def test_the_engine_binds_localhost_on_a_random_port() -> None:
    a, b = discovery.bind_localhost(), discovery.bind_localhost()
    try:
        assert a.getsockname()[0] == "127.0.0.1"
        assert a.getsockname()[1] != 0
        assert a.getsockname()[1] != b.getsockname()[1]
    finally:
        a.close()
        b.close()


def test_the_token_is_32_random_bytes() -> None:
    import base64

    t1, t2 = discovery.new_token(), discovery.new_token()
    assert t1 != t2
    assert len(base64.urlsafe_b64decode(t1 + "=")) == 32
