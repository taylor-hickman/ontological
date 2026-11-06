"""Servers for the scanner tests, and a keychain that isn't the real one.

Postgres runs on the bundled binaries (ONTOLOGICAL_PG_BIN, as in tests/store), in a cluster of
its own that asks the `reader` role for a password and trusts the rest. MySQL is any server
named by ONTOLOGICAL_MYSQL_URL (`mysql://user:password@host:port`, a user that can create
databases and users); CI starts the runner's own. Without them those tests skip, unless
ONTOLOGICAL_PG_REQUIRED=1 or ONTOLOGICAL_MYSQL_REQUIRED=1 turns a skip into a failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import keyring
import pytest
from fakes import MemoryKeyring


def _required(var: str) -> bool:
    return os.environ.get(var) == "1"


def _missing(what: str, required_var: str) -> None:
    if _required(required_var):
        pytest.fail(f"{what}, and {required_var}=1")
    pytest.skip(what)


@pytest.fixture
def keychain() -> Iterator[MemoryKeyring]:
    before = keyring.get_keyring()
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    try:
        yield ring
    finally:
        keyring.set_keyring(before)


@dataclass(frozen=True)
class PgCluster:
    bin: Path
    socket_dir: Path

    def psql(self, sql: str, *, db: str = "postgres") -> str:
        p = subprocess.run(
            [
                str(self.bin / "psql"), "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1",
                "-h", str(self.socket_dir), "-U", "postgres", "-d", db, "-c", sql,
            ],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        return p.stdout.strip()


@pytest.fixture(scope="session")
def pg_cluster() -> Iterator[PgCluster]:
    value = os.environ.get("ONTOLOGICAL_PG_BIN")
    if not value:
        _missing(
            "set ONTOLOGICAL_PG_BIN to a PostgreSQL 17 bin/ directory", "ONTOLOGICAL_PG_REQUIRED"
        )
    bin_dir = Path(value or "").resolve()
    root = Path(tempfile.mkdtemp(prefix="src-pg-", dir="/tmp"))  # the socket path must be short
    data, sock = root / "data", root / "sock"
    sock.mkdir(mode=0o700)

    def run(*cmd: str) -> None:
        subprocess.run(cmd, capture_output=True, text=True, check=True)

    run(str(bin_dir / "initdb"), "-D", str(data), "-E", "UTF8", "--locale=C", "-U", "postgres",
        "--auth-local=trust", "--auth-host=reject")  # fmt: skip
    (data / "pg_hba.conf").write_text("local all reader scram-sha-256\nlocal all all trust\n")
    run(str(bin_dir / "pg_ctl"), "-D", str(data), "-l", str(root / "log"), "-w",
        "-o", f"-k {sock} -c listen_addresses=''", "start")  # fmt: skip
    try:
        yield PgCluster(bin_dir, sock)
    finally:
        subprocess.run(
            [str(bin_dir / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"],
            capture_output=True,
        )
        shutil.rmtree(root, ignore_errors=True)


@dataclass(frozen=True)
class MySQLServer:
    host: str
    port: int
    user: str
    password: str

    @property
    def dsn(self) -> str:
        """For the test's own, writable DuckDB attach."""
        return f"host={self.host} port={self.port} user={self.user} password={self.password}"


@pytest.fixture(scope="session")
def mysql_server() -> MySQLServer:
    value = os.environ.get("ONTOLOGICAL_MYSQL_URL")
    if not value:
        _missing("set ONTOLOGICAL_MYSQL_URL to mysql://user:password@host:port",
                 "ONTOLOGICAL_MYSQL_REQUIRED")  # fmt: skip
    url = urlparse(value or "")
    return MySQLServer(
        url.hostname or "127.0.0.1",
        url.port or 3306,
        unquote(url.username or "root"),
        unquote(url.password or ""),
    )
