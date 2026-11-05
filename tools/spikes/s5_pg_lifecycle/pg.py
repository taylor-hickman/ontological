"""Prototype of the engine's `store/pg.py` (ARCHITECTURE.md §5.3), for spike S5.

Runs the bundled Postgres for one `<appData>/Ontological` directory: first launch (initdb), start,
adopt a server a previous engine left running, stop with a quit dump, pre-migration dumps and
restore, and the major-version move. Standard library only; it drives the bundled binaries
(`pg_ctl`, `psql`, `pg_dump`, `pg_restore`) as subprocesses. M0.3 rewrites it test-first.

As a script it is a stand-in engine, so the lifecycle cases can kill it:

    python3 pg.py <appData> <bin>    # ensure_running, print one JSON line, wait;
                                     # SIGTERM or SIGINT: quit dump, stop, exit 0
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

USER = "ontological"
DATABASE = "ontological"
# With listen_addresses='' the port only names the socket file (.s.PGSQL.<port>).
PORT = 5432
# macOS sockaddr_un.sun_path is 104 bytes including the terminating NUL.
MAX_SOCKET_PATH = 103
SETTINGS = {
    "listen_addresses": "",
    "shared_buffers": "128MB",
    "max_connections": "30",
}
START_TIMEOUT_S = 120  # pg_ctl -t: covers WAL replay after a crash
KEEP_DUMPS = 10


class PgError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    """Where things live (§5.1). `bin` is the bundled Postgres's bin/ directory."""

    app_data: Path
    bin: Path

    @property
    def pgdata(self) -> Path:
        return self.app_data / "pgdata"

    @property
    def pgsocket(self) -> Path:
        return self.app_data / "pgsocket"

    @property
    def backups(self) -> Path:
        return self.app_data / "backups"

    @property
    def log(self) -> Path:
        return self.app_data / "logs" / "postgres.log"

    @property
    def lock(self) -> Path:
        return self.app_data / "pg.lock"

    def tool(self, name: str) -> str:
        return str(self.bin / name)


@dataclass(frozen=True)
class PidFile:
    """postmaster.pid: pid, data dir, start time, port, socket dir, listen address, shmem key,
    status. The status line is `starting`, `ready` or `stopping` (PG 10+)."""

    pid: int
    data_dir: str
    port: int
    socket_dir: str
    status: str


@dataclass(frozen=True)
class Server:
    postmaster_pid: int
    action: str  # "initdb+start", "start", "adopt", "upgrade"
    notes: tuple[str, ...] = ()


# --- small helpers -----------------------------------------------------------------------------


def run(
    cmd: Sequence[str], *, check: bool = True, **kw: object
) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(list(cmd), capture_output=True, text=True, stdin=subprocess.DEVNULL, **kw)  # type: ignore[call-overload]
    if check and p.returncode != 0:
        raise PgError(
            f"{Path(cmd[0]).name} failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}"
        )
    return p


def binary_major(layout: Layout) -> int:
    out = run([layout.tool("postgres"), "--version"]).stdout  # "postgres (PostgreSQL) 17.11"
    m = re.search(r"\) (\d+)", out)
    if not m:
        raise PgError(f"can't read the version from {out!r}")
    return int(m.group(1))


def data_major(layout: Layout) -> int | None:
    f = layout.pgdata / "PG_VERSION"
    return int(f.read_text().strip()) if f.exists() else None


def read_pid_file(pgdata: Path) -> PidFile | None:
    try:
        lines = (pgdata / "postmaster.pid").read_text().splitlines()
    except FileNotFoundError:
        return None
    if len(lines) < 5 or not lines[0].strip().isdigit():
        return None  # being written, or truncated by a crash; Postgres treats it as stale too
    return PidFile(
        pid=int(lines[0]),
        data_dir=lines[1],
        port=int(lines[3]) if lines[3].strip().isdigit() else PORT,
        socket_dir=lines[4].strip(),
        status=lines[7].strip() if len(lines) > 7 else "",
    )


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, another user's
    return True


def process_command(pid: int) -> str:
    """Full executable path on macOS (`ps -o comm=`); '' if the process is gone."""
    return run(["ps", "-o", "comm=", "-p", str(pid)], check=False).stdout.strip()


@contextlib.contextmanager
def locked(layout: Layout) -> Iterator[None]:
    """Serialise initdb, start, adopt and stop between engines. flock is dropped by the kernel
    when its holder dies, so a kill -9 never leaves it held."""
    layout.app_data.mkdir(parents=True, exist_ok=True)
    with open(layout.lock, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def psql(
    layout: Layout, sql: str, *, db: str = DATABASE, socket_dir: str | None = None, port: int = PORT
) -> str:
    p = run(
        [
            layout.tool("psql"), "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1",
            "-h", socket_dir or str(layout.pgsocket), "-p", str(port), "-U", USER, "-d", db,
            "-c", sql,
        ]
    )  # fmt: skip
    return p.stdout.strip()


# --- state of the data directory ---------------------------------------------------------------


@dataclass(frozen=True)
class Running:
    pid_file: PidFile


@dataclass(frozen=True)
class Stale:
    pid_file: PidFile
    reason: str


def inspect_pid_file(
    layout: Layout, *, pgdata: Path | None = None, wait_s: float = START_TIMEOUT_S
) -> Running | Stale | None:
    """Decide whether postmaster.pid names our live server (§5.3, Adopt).

    A live pid is not enough: after a reboot or a crash the pid can belong to any process,
    including another cluster's postgres, and `pg_ctl status` (which only does kill(pid, 0))
    reports it as running. It is ours only if the process is a postgres and a connection
    through the socket it advertises reports this data directory.
    """
    pgdata = pgdata or layout.pgdata
    pf = read_pid_file(pgdata)
    if pf is None:
        return None
    if not pid_alive(pf.pid):
        return Stale(pf, f"pid {pf.pid} is not running")
    cmd = process_command(pf.pid)
    if Path(cmd).name != "postgres":
        return Stale(pf, f"pid {pf.pid} is {cmd or 'unknown'}, not postgres")
    deadline = time.monotonic() + wait_s
    while True:
        # A starting server (WAL replay) or one shutting down refuses connections for a while.
        p = run(
            [
                layout.tool("psql"), "-X", "-t", "-A", "-h", pf.socket_dir, "-p", str(pf.port),
                "-U", USER, "-d", "postgres", "-c", "SHOW data_directory",
            ],
            check=False,
        )  # fmt: skip
        if p.returncode == 0:
            ours = Path(p.stdout.strip()).resolve() == pgdata.resolve()
            return Running(pf) if ours else Stale(pf, f"pid {pf.pid} serves {p.stdout.strip()}")
        if not pid_alive(pf.pid):
            return Stale(pf, f"pid {pf.pid} exited while we waited for it")
        busy = ("starting up", "shutting down", "not yet accepting connections", "recovery")
        if not any(s in p.stderr for s in busy):
            # Another cluster's postgres whose socket isn't ours, or a socket that is gone.
            return Stale(pf, f"pid {pf.pid} is a postgres but not this server: {p.stderr.strip()}")
        if time.monotonic() > deadline:
            raise PgError(f"postgres pid {pf.pid} is still {pf.status or 'busy'} after {wait_s}s")
        time.sleep(0.2)


# --- lifecycle ---------------------------------------------------------------------------------


def initdb(layout: Layout, target: Path | None = None) -> None:
    """First launch (§5.3). initdb into a sibling directory and rename it into place, so a
    crash part-way never leaves a pgdata/ that looks initialised. With `target`, initdb into
    that directory and leave it there (the major-version staging cluster)."""
    tmp = target or layout.pgdata.with_name("pgdata.initdb")
    shutil.rmtree(tmp, ignore_errors=True)
    run(
        [
            layout.tool("initdb"), "-D", str(tmp), "-E", "UTF8", "--locale=C", "-U", USER,
            "--auth-local=trust", "--auth-host=reject",
        ]
    )  # fmt: skip
    if target is None:
        tmp.rename(layout.pgdata)


def start(layout: Layout, pgdata: Path | None = None) -> None:
    sock = layout.pgsocket / f".s.PGSQL.{PORT}"
    if len(str(sock).encode()) > MAX_SOCKET_PATH:
        raise PgError(f"socket path is {len(str(sock).encode())} bytes, over {MAX_SOCKET_PATH}")
    layout.pgsocket.mkdir(mode=0o700, parents=True, exist_ok=True)
    layout.pgsocket.chmod(0o700)
    layout.log.parent.mkdir(parents=True, exist_ok=True)
    # pg_ctl hands -o to /bin/sh, so each value is shell-quoted: <appData> has a space in it.
    opts = ["-k", str(layout.pgsocket), "-p", str(PORT)]
    for k, v in SETTINGS.items():
        opts += ["-c", f"{k}={v}"]
    run(
        [
            layout.tool("pg_ctl"), "start", "-D", str(pgdata or layout.pgdata),
            "-l", str(layout.log),
            "-w", "-t", str(START_TIMEOUT_S), "-o", shlex.join(opts),
        ]
    )  # fmt: skip


def ensure_running(
    layout: Layout, *, confirm_stale_dump: Callable[[str], bool] = lambda _: True
) -> Server:
    """Start or adopt the store's server. Safe to call from several engines at once: the lock
    serialises them and every caller ends up on the same postmaster."""
    with locked(layout):
        notes: list[str] = []
        if resumed := resume_upgrade(layout):
            notes.append(resumed)
        major = binary_major(layout)
        have = data_major(layout)
        if have is None:
            initdb(layout)
            start(layout)
            create_store(layout)
            return Server(_postmaster_pid(layout), "initdb+start")
        if have != major:
            return upgrade_major(layout, have, confirm_stale_dump, notes)

        state = inspect_pid_file(layout)
        if isinstance(state, Running):
            return Server(state.pid_file.pid, "adopt")
        if isinstance(state, Stale):
            # Postgres removes a lock file whose pid is dead, but refuses to start while that
            # pid is alive, whatever the process is.
            notes.append(f"stale postmaster.pid: {state.reason}")
            (layout.pgdata / "postmaster.pid").unlink()
        clear_stale_socket(layout)
        start(layout)
        return Server(_postmaster_pid(layout), "start", tuple(notes))


def resume_upgrade(layout: Layout) -> str | None:
    """Finish or discard a major-version move that a crash interrupted (see upgrade_major).

    pgdata.upgraded/ is a complete, stopped cluster. If pgdata/ is missing, the crash came
    after the old cluster was moved aside, so finish the swap. If pgdata/ is still the old
    cluster, delete pgdata.upgraded/ and let the move run again from the same dump.
    pgdata.upgrade/ is a staging cluster that never finished restoring: delete it too."""
    done = layout.pgdata.with_name("pgdata.upgraded")
    if done.exists():
        if not layout.pgdata.exists():
            done.rename(layout.pgdata)
            return "finished an interrupted major-version move"
        shutil.rmtree(done)
        return "discarded a major-version cluster that was never swapped in"
    staging = layout.pgdata.with_name("pgdata.upgrade")
    if staging.exists():
        # The crash may have left the staging server running.
        state = inspect_pid_file(layout, pgdata=staging)
        if isinstance(state, Running):
            os.kill(state.pid_file.pid, signal.SIGQUIT)  # immediate shutdown
            _wait_gone(state.pid_file.pid, 60)
        shutil.rmtree(staging)
        return "discarded an unfinished major-version staging cluster"
    return None


def _postmaster_pid(layout: Layout) -> int:
    pf = read_pid_file(layout.pgdata)
    if pf is None:
        raise PgError("server started but wrote no postmaster.pid")
    return pf.pid


def create_store(layout: Layout) -> None:
    """What the two Alembic chains need before their first revision."""
    psql(layout, f"CREATE DATABASE {DATABASE}", db="postgres")
    psql(layout, "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm")
    psql(
        layout,
        "CREATE SCHEMA ops; CREATE TABLE ops.version (rev int NOT NULL);"
        "INSERT INTO ops.version VALUES (0)",
    )


def stop(layout: Layout, *, quit_dump: bool = True) -> Path | None:
    """Engine shutdown (§5.3, Stop): quit dump, then a fast stop."""
    with locked(layout):
        state = inspect_pid_file(layout)
        if not isinstance(state, Running):
            return None
        path = dump(layout, "quit") if quit_dump else None
        run(
            [
                layout.tool("pg_ctl"),
                "stop",
                "-D",
                str(layout.pgdata),
                "-m",
                "fast",
                "-w",
                "-t",
                "60",
            ]
        )
        return path


# --- dumps, migrations, restore ----------------------------------------------------------------


def dump(layout: Layout, kind: str, *, socket_dir: str | None = None, port: int = PORT) -> Path:
    """pg_dump -Fc into backups/. Written under a temporary name and renamed when complete, so
    a crash mid-dump never leaves a truncated file that looks like the newest dump."""
    layout.backups.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S") + f"{time.time() % 1:.6f}"[1:]
    final = layout.backups / f"ontology-{ts}-{kind}.dump"
    part = final.with_suffix(".dump.part")
    run(
        [
            layout.tool("pg_dump"), "-Fc", "-h", socket_dir or str(layout.pgsocket),
            "-p", str(port),
            "-U", USER, "-d", DATABASE, "-f", str(part),
        ]
    )  # fmt: skip
    part.rename(final)
    for old in sorted(layout.backups.glob("ontology-*.dump"))[:-KEEP_DUMPS]:
        old.unlink()
    return final


def newest_dump(layout: Layout) -> Path | None:
    dumps = sorted(layout.backups.glob("ontology-*.dump"))
    return dumps[-1] if dumps else None


def restore(layout: Layout, path: Path) -> None:
    """Put the store back to a dump. The database is dropped and recreated: `pg_restore --clean`
    only drops objects that are in the dump, so a table a failed migration created survives it."""
    psql(layout, f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)", db="postgres")
    psql(layout, f"CREATE DATABASE {DATABASE}", db="postgres")
    run(
        [
            layout.tool("pg_restore"), "--exit-on-error", "--single-transaction",
            "-h", str(layout.pgsocket), "-p", str(PORT), "-U", USER, "-d", DATABASE, str(path),
        ]
    )  # fmt: skip


class MigrationFailed(PgError):
    def __init__(self, rev: int, error: str, restored_from: Path) -> None:
        super().__init__(f"revision {rev} failed, restored {restored_from.name}: {error}")
        self.rev = rev
        self.restored_from = restored_from


def migrate(layout: Layout, revisions: Sequence[str]) -> int:
    """Apply pending revisions (§5.3, Migrations). Revision n is revisions[n-1]. Each runs in its
    own transaction, as Alembic does with transaction_per_migration, so an earlier one has
    committed by the time a later one fails. That is the case the dump is for."""
    current = int(psql(layout, "SELECT rev FROM ops.version"))
    pending = list(enumerate(revisions, start=1))[current:]
    if not pending:
        return current
    before = dump(layout, "premigrate")
    for rev, sql in pending:
        try:
            psql(layout, f"BEGIN; {sql}; UPDATE ops.version SET rev = {rev}; COMMIT;")
        except PgError as e:
            restore(layout, before)
            raise MigrationFailed(rev, str(e), before) from e
    return len(revisions)


# --- major-version move ------------------------------------------------------------------------


def dump_is_current(old_pgdata: Path, path: Path) -> tuple[bool, str]:
    """Is the newest dump the last state of the old data directory? Only a quit dump written
    after the old server's last start, followed by a clean stop, is. postmaster.opts is
    rewritten at every start; postmaster.pid is removed by a clean stop."""
    if (old_pgdata / "postmaster.pid").exists():
        return False, "the old server did not stop cleanly; changes after the last dump are lost"
    if not path.name.endswith("-quit.dump"):
        return False, f"the newest dump ({path.name}) is not a quit dump"
    opts = old_pgdata / "postmaster.opts"
    if opts.exists() and path.stat().st_mtime < opts.stat().st_mtime:
        return False, "the newest dump is older than the old server's last start"
    return True, ""


def upgrade_major(
    layout: Layout,
    old: int,
    confirm_stale_dump: Callable[[str], bool],
    notes: list[str] | None = None,
) -> Server:
    """§5.3, Major-version upgrades. If the old server is still running (its engine was killed
    and an app update replaced the binaries), dump it live first: pg_dump reads older majors, so
    nothing is lost. Otherwise restore the newest dump, after saying so if it is stale."""
    notes = [*(notes or []), f"data directory is PostgreSQL {old}"]
    state = inspect_pid_file(layout)
    if isinstance(state, Running):
        pf = state.pid_file
        dump(layout, "quit", socket_dir=pf.socket_dir, port=pf.port)
        os.kill(pf.pid, signal.SIGINT)  # fast shutdown; the old pg_ctl may be gone
        _wait_gone(pf.pid, 60)
        notes.append(f"dumped the running PostgreSQL {old} server live, then stopped it")
    path = newest_dump(layout)
    if path is None:
        raise PgError(f"PostgreSQL {old} data and no dump to restore")
    ok, why = dump_is_current(layout.pgdata, path)
    if not ok:
        notes.append(f"stale dump: {why}")
        if not confirm_stale_dump(why):
            raise PgError(f"upgrade cancelled: {why}")
    # Build and fill the new cluster beside the old one; pgdata/ stays the old cluster until the
    # restore has succeeded. A crash or a failed restore before the swap leaves pgdata/ as it
    # was, so the next start tries the move again instead of finding no pgdata/ and running a
    # first launch on an empty store.
    staging = layout.pgdata.with_name("pgdata.upgrade")
    clear_stale_socket(layout)
    initdb(layout, staging)
    start(layout, staging)
    try:
        psql(layout, f"CREATE DATABASE {DATABASE}", db="postgres")
        run(
            [
                layout.tool("pg_restore"), "--exit-on-error", "--single-transaction",
                "-h", str(layout.pgsocket), "-p", str(PORT), "-U", USER, "-d", DATABASE,
                str(path),
            ]
        )  # fmt: skip
    except PgError:
        _stop_dir(layout, staging, "immediate")
        shutil.rmtree(staging)
        raise
    _stop_dir(layout, staging, "fast")
    # Two renames. pgdata.upgraded/ marks the staging cluster complete, so resume_upgrade can
    # finish the swap if a crash lands between moving the old one aside and moving this in.
    done = staging.rename(layout.pgdata.with_name("pgdata.upgraded"))
    aside = layout.pgdata.rename(
        layout.pgdata.with_name(f"pgdata-{old}-{time.strftime('%Y%m%dT%H%M%S')}")
    )
    done.rename(layout.pgdata)
    start(layout)
    notes += [f"restored {path.name}", f"old data kept in {aside.name}"]
    return Server(_postmaster_pid(layout), "upgrade", tuple(notes))


def _stop_dir(layout: Layout, pgdata: Path, mode: str) -> None:
    run([layout.tool("pg_ctl"), "stop", "-D", str(pgdata), "-m", mode, "-w", "-t", "60"])


def clear_stale_socket(layout: Layout) -> None:
    """Remove the socket and its lock file (`.s.PGSQL.<port>.lock`). Call only when no server of
    ours is running: the socket directory is ours alone, so they are left from a crash. The
    socket lock holds the postmaster's pid and Postgres checks it the same way as
    postmaster.pid, so a reused pid there blocks the start too."""
    for f in layout.pgsocket.glob(".s.PGSQL.*"):
        f.unlink(missing_ok=True)


def _wait_gone(pid: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while pid_alive(pid):
        if time.monotonic() > deadline:
            raise PgError(f"pid {pid} still running after {timeout_s}s")
        time.sleep(0.1)


# --- stand-in engine ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    layout = Layout(Path(argv[1]), Path(argv[2]))
    t0 = time.monotonic()
    server = ensure_running(layout)
    print(
        json.dumps(
            {
                "engine_pid": os.getpid(),
                "postmaster_pid": server.postmaster_pid,
                "action": server.action,
                "notes": server.notes,
                "seconds": round(time.monotonic() - t0, 3),
            }
        ),
        flush=True,
    )
    stopping = False

    def on_signal(_sig: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    while not stopping:
        signal.pause()
    path = stop(layout)
    print(json.dumps({"stopped": True, "quit_dump": path.name if path else None}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
