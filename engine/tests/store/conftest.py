"""Fixtures for the Postgres lifecycle tests (ARCHITECTURE.md §5.3 and §12).

The tests drive the bundled binaries that tools/spikes/s4_postgres builds:

    ONTOLOGICAL_PG_BIN=<trimmed 17 tree>/bin          every lifecycle test
    ONTOLOGICAL_PG16_BIN=<a PostgreSQL 16 build>/bin   the major-version tests

Without them those tests skip, unless ONTOLOGICAL_PG_REQUIRED=1 (set in CI), which turns a
missing binary into a failure so a test can't skip quietly.

Each engine is engine_stub.py run in its own session, so a test can kill -9 it the way a crash
would. Every <appData> has a space in its path, as `Application Support` does, under a short
root in /tmp: the socket path has to fit in 103 bytes.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from ontological.store import dal, pg

STUB = Path(__file__).with_name("engine_stub.py")
REQUIRED = os.environ.get("ONTOLOGICAL_PG_REQUIRED") == "1"


def _bin(var: str, major: int) -> Path:
    value = os.environ.get(var)
    if not value:
        if REQUIRED:
            pytest.fail(f"{var} is not set and ONTOLOGICAL_PG_REQUIRED=1")
        pytest.skip(f"set {var} to a PostgreSQL {major} bin/ directory")
    path = Path(value).resolve()
    got = pg.binary_major(pg.Layout(Path("/nonexistent"), path))
    assert got == major, f"{var} is PostgreSQL {got}, expected {major}"
    return path


@pytest.fixture(scope="session")
def bin17() -> Path:
    return _bin("ONTOLOGICAL_PG_BIN", 17)


@pytest.fixture(scope="session")
def bin16() -> Path:
    return _bin("ONTOLOGICAL_PG16_BIN", 16)


class EngineFailed(Exception):
    """The stub engine exited with an error instead of becoming ready."""

    def __init__(self, info: dict[str, Any]) -> None:
        super().__init__(f"{info.get('type')}: {info.get('error')}")
        self.info = info


class Engine:
    """engine_stub.py: ensure_running, report, wait; SIGTERM stops with a quit dump."""

    def __init__(self, layout: pg.Layout, *args: str, ready: bool = True) -> None:
        self.layout = layout
        self.info: dict[str, Any] = {}
        self.proc = subprocess.Popen(
            [sys.executable, str(STUB), str(layout.app_data), str(layout.bin), *args],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        ENGINES.append(self.proc)
        if ready:
            self.wait_ready()

    @staticmethod
    def many(layout: pg.Layout, n: int) -> list[Engine]:
        """n engines started at once, before any of them is ready."""
        engines = [Engine(layout, ready=False) for _ in range(n)]
        for e in engines:
            e.wait_ready()
        return engines

    def wait_ready(self) -> None:
        self.info = self._read()
        if "error" in self.info:
            self.proc.wait(10)
            raise EngineFailed(self.info)

    def _read(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            self.proc.wait(10)
            return {"error": f"exited {self.proc.returncode} without a word", "type": "?"}
        return cast(dict[str, Any], json.loads(line))

    @property
    def action(self) -> str:
        return str(self.info["action"])

    @property
    def postmaster(self) -> int:
        return int(self.info["postmaster_pid"])

    @property
    def notes(self) -> str:
        return " | ".join(self.info["notes"])

    def kill9(self) -> None:
        """kill -9 of the engine's whole process group, as a crash or a force quit does."""
        os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait(10)

    def term_error(self) -> dict[str, Any]:
        """SIGTERM, expecting the stop to report an error."""
        self.proc.send_signal(signal.SIGTERM)
        out = self._read()
        self.proc.wait(60)
        assert self.proc.returncode == 1 and "error" in out, out
        return out

    def term(self) -> dict[str, Any]:
        self.proc.send_signal(signal.SIGTERM)
        out = self._read()
        self.proc.wait(60)
        assert self.proc.returncode == 0, out
        return out


ENGINES: list[subprocess.Popen[str]] = []


def run_stub(layout: pg.Layout, *args: str) -> int:
    """Run engine_stub.py to completion (a --die-at or one-shot mode); its exit code."""
    p = subprocess.run(
        [sys.executable, str(STUB), str(layout.app_data), str(layout.bin), *args],
        capture_output=True,
        text=True,
        timeout=180,
        start_new_session=True,
    )
    return p.returncode


class Apps:
    """Makes <appData> directories under one short root and cleans up what the tests leave."""

    def __init__(self, root: Path, bin17: Path) -> None:
        self.root = root
        self.bin17 = bin17
        self.layouts: list[pg.Layout] = []
        self.decoys: list[subprocess.Popen[bytes]] = []

    def layout(self, name: str, bin: Path | None = None) -> pg.Layout:
        lay = pg.Layout(self.root / name / "App Support" / "Ontological", bin or self.bin17)
        self.layouts.append(lay)
        return lay

    def decoy(self) -> subprocess.Popen[bytes]:
        """An unrelated live process whose pid a stale postmaster.pid can name."""
        p = subprocess.Popen(["sleep", "600"])
        self.decoys.append(p)
        return p

    def cleanup(self) -> None:
        for p in ENGINES:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
        ENGINES.clear()
        for d in self.decoys:
            d.kill()
            d.wait()
        for lay in self.layouts:
            stop_servers(lay.app_data)


def stop_servers(app_data: Path) -> None:
    """Immediate shutdown of any server a test left running in <appData>, waiting for it."""
    for data in app_data.glob("pgdata*"):
        pf = pg.read_pid_file(data)
        if pf and pg.pid_alive(pf.pid) and pg.is_postgres(pf.pid):
            os.kill(pf.pid, signal.SIGQUIT)
            with contextlib.suppress(pg.PgError):
                pg.wait_gone(pf.pid, 30)


@pytest.fixture
def apps(bin17: Path) -> Iterator[Apps]:
    root = Path(tempfile.mkdtemp(prefix="pg-", dir="/tmp"))
    a = Apps(root, bin17)
    try:
        yield a
    finally:
        a.cleanup()
        shutil.rmtree(root, ignore_errors=True)


# --- helpers the cases share --------------------------------------------------------------------


def sql(lay: pg.Layout, q: str, db: str = pg.DATABASE) -> str:
    return pg.psql(lay, q, db=db)


def seed(lay: pg.Layout, start: int, n: int) -> None:
    """Rows with a vector column and an HNSW index, as the vdb schema has."""
    sql(
        lay,
        "CREATE TABLE IF NOT EXISTS items (id int PRIMARY KEY, name text, embedding vector(3));"
        "CREATE INDEX IF NOT EXISTS items_hnsw ON items USING hnsw (embedding vector_cosine_ops);"
        f"INSERT INTO items SELECT i, 'table_' || i, ARRAY[sin(i), cos(i), i % 7]::vector "
        f"FROM generate_series({start}, {start + n - 1}) i",
    )


def count(lay: pg.Layout) -> int:
    return int(sql(lay, "SELECT count(*) FROM items"))


def nearest(lay: pg.Layout) -> str:
    return sql(
        lay,
        "SET enable_seqscan = off; SELECT id FROM items ORDER BY embedding <=> '[0,1,3]' LIMIT 1",
    )


def crash_postmaster(pid: int) -> None:
    """kill -9 the postmaster and wait for its backends to notice and exit. Until they do they
    keep its shared memory attached, and Postgres refuses to start on top of them."""
    os.kill(pid, signal.SIGKILL)
    pg.wait_gone(pid, 30)
    deadline = time.monotonic() + 30
    while _orphaned_backends():
        assert time.monotonic() < deadline, "postgres children still running 30 s after"
        time.sleep(0.1)


def _orphaned_backends() -> list[str]:
    # Backends' titles don't name the data dir; on a test machine an orphaned postgres child
    # (parent pid 1) is left from our kill.
    out = subprocess.run(["ps", "-axo", "ppid=,command="], capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if ln.strip().startswith("1 ") and "postgres: " in ln]


def backends(postmaster: int) -> list[int]:
    """Client backends of `postmaster` (their title starts with "postgres: <user> ")."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True)
    return [
        int(f[0])
        for f in (ln.split(None, 2) for ln in out.stdout.splitlines())
        if len(f) == 3 and f[1] == str(postmaster) and f[2].startswith(f"postgres: {pg.USER} ")
    ]


def postmasters_for(lay: pg.Layout) -> list[str]:
    # -ww: procps (Linux) cuts piped output at 80 columns, which drops the -D path.
    out = subprocess.run(["ps", "-axwwo", "pid=,command="], capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if f"-D {lay.pgdata}" in ln and "pg_ctl" not in ln]


def log_size(lay: pg.Layout) -> int:
    return lay.log.stat().st_size if lay.log.exists() else 0


def log_since(lay: pg.Layout, offset: int) -> str:
    with open(lay.log, "rb") as f:
        f.seek(offset)
        return f.read().decode(errors="replace")


def forge_pid(lay: pg.Layout, pid: int) -> None:
    """Point postmaster.pid and the socket lock at `pid`, as after a reboot that reused it."""
    for f in (lay.pgdata / "postmaster.pid", lay.pgsocket / f".s.PGSQL.{pg.PORT}.lock"):
        lines = f.read_text().splitlines()
        lines[0] = str(pid)
        f.write_text("\n".join(lines) + "\n")


@pytest.fixture
def store(apps: Apps, monkeypatch: pytest.MonkeyPatch) -> Iterator[pg.Layout]:
    """A migrated store with the DAL connected to it. The variables are restored afterwards."""
    lay = apps.layout("dal")
    pg.ensure_running(lay)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    for name, value in dal.environ(lay).items():
        monkeypatch.setenv(name, value)
    dal.connect(lay)
    try:
        yield lay
    finally:
        dal.close()
        pg.stop(lay, quit_dump=False)


CHINOOK_SQL = Path(__file__).parents[1] / "upstream_fixtures/dev_tools/fixtures/sql/chinook.sql"


@pytest.fixture(scope="module")
def chinook(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Chinook as a SQLite file, built from upstream's fixture SQL (MIT, THIRD_PARTY_NOTICES.md)."""
    path = tmp_path_factory.mktemp("src") / "chinook.sqlite"
    con = sqlite3.connect(path)
    try:
        con.executescript(CHINOOK_SQL.read_text(encoding="utf-8-sig"))  # the script has a BOM
    finally:
        con.close()
    return path
