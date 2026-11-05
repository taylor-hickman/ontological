"""How clients find the engine (ARCHITECTURE.md §4.3, §4.4): `<appData>/engine.json` names the
running engine's pid, port, bearer token and version, at mode 0600, and `<appData>/engine.lock`
is the flock held while engine.json is read to decide whether an engine is running, and while an
engine starts and writes it.

A client that finds a live engine attaches to it; otherwise it starts one and waits for
engine.json to name a live engine. It doesn't hold engine.lock while it waits: the new engine
takes the lock itself, and if another engine won the race, it says so and exits, so two clients
starting at once still get one engine.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import socket
import urllib.request
from collections.abc import Generator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any, cast

ENGINE_JSON = "engine.json"
ENGINE_LOCK = "engine.lock"
TOKEN_BYTES = 32
HEALTH_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class EngineInfo:
    pid: int
    port: int
    token: str
    version: str


def new_token() -> str:
    """32 random bytes, URL-safe base64 without padding (43 characters)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def bind_localhost() -> socket.socket:
    """A listening socket on 127.0.0.1 at a port the OS picks."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(128)
    except OSError:
        s.close()
        raise
    return s


class EngineLock:
    """<appData>/engine.lock, an flock. The kernel drops it when its holder dies, so a crash
    never leaves it held. `acquire` and `release` do nothing if it is already held or free."""

    def __init__(self, app_data: Path) -> None:
        self.path = app_data / ENGINE_LOCK
        self._file: IO[str] | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a")  # noqa: SIM115 - held until release()
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
        except BaseException:
            f.close()
            raise
        self._file = f

    def release(self) -> None:
        if self._file is not None:
            f, self._file = self._file, None
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


@contextlib.contextmanager
def engine_lock(app_data: Path) -> Generator[None]:
    lock = EngineLock(app_data)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def write_engine_json(app_data: Path, info: EngineInfo) -> None:
    """Write engine.json at mode 0600 whatever the umask, under a temporary name first, so a
    reader never sees half a file."""
    path = app_data / ENGINE_JSON
    part = path.with_name(f"{ENGINE_JSON}.{os.getpid()}.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, json.dumps(asdict(info)).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    part.replace(path)


def read_engine_json(app_data: Path) -> EngineInfo | None:
    """What engine.json says, or None if it is missing or damaged. It says nothing about whether
    that engine is still running: see `find_engine`."""
    try:
        data: Any = json.loads((app_data / ENGINE_JSON).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    fields = cast(dict[str, Any], data)
    pid, port, token, version = (fields.get(k) for k in ("pid", "port", "token", "version"))
    if not (
        isinstance(pid, int)
        and isinstance(port, int)
        and isinstance(token, str)
        and isinstance(version, str)
    ):
        return None
    return EngineInfo(pid, port, token, version)


def remove_engine_json(app_data: Path, *, pid: int) -> None:
    """Remove engine.json if it still names `pid`. Call under engine_lock."""
    info = read_engine_json(app_data)
    if info is not None and info.pid == pid:
        (app_data / ENGINE_JSON).unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def answers(info: EngineInfo, timeout_s: float = HEALTH_TIMEOUT_S) -> bool:
    """Whether the engine engine.json names answers /health with its own pid. After a crash and
    a reboot the pid can belong to any process, and the port to any server."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{info.port}/health",
        headers={"Authorization": f"Bearer {info.token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            body: Any = json.loads(r.read())
    except (OSError, ValueError):  # refused, timed out, an HTTP error, not JSON
        return False
    return isinstance(body, dict) and cast(dict[str, Any], body).get("pid") == info.pid


def find_engine(app_data: Path) -> EngineInfo | None:
    """The live engine engine.json names, or None. Call under engine_lock to decide whether to
    start one."""
    info = read_engine_json(app_data)
    if info is None or not _pid_alive(info.pid):
        return None
    return info if answers(info) else None
