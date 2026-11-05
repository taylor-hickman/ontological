"""The bundled Postgres's lifecycle (ARCHITECTURE.md §5.3).

Runs the store's server for one `<appData>/Ontological` directory: first launch (initdb), start,
adopting a server a previous engine left running, stop with the quit dump, dumps and restore, the
major-version move, and the pause when the disk is full. It drives the bundled binaries
(`initdb`, `pg_ctl`, `psql`, `pg_dump`, `pg_restore`) as subprocesses and uses only the standard
library. Grown from spike S5's prototype (tools/spikes/s5_pg_lifecycle/pg.py).

The engine calls `ensure_running` (or `wait_until_running`, which waits out a full disk) before
it serves requests, and `stop` on shutdown. Several engines may call them at once: `pg.lock`
serialises them, and they all end up on one postmaster.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

USER = "ontological"
DATABASE = "ontological"
# restore() builds the restored store here and renames it over DATABASE only once it is complete.
RESTORE_DATABASE = f"{DATABASE}_restore"
# With listen_addresses='' the port only names the socket file (.s.PGSQL.<port>).
PORT = 5432
# macOS sockaddr_un.sun_path is 104 bytes including the terminating NUL.
MAX_SOCKET_PATH = 103
SETTINGS = {
    "listen_addresses": "",
    "shared_buffers": "128MB",
    "max_connections": "30",
}
EXTENSIONS = ("vector", "pg_trgm")
START_TIMEOUT_S = 120  # pg_ctl -t: covers WAL replay after a crash
STOP_TIMEOUT_S = 60
KEEP_DUMPS = 10  # §5.7 retention: the newest 10, and never the dump just written
# Postgres needs room for a new 16 MB WAL segment; with less free than that, a server that has
# stopped is taken to have stopped for want of space even if its log couldn't say so.
MIN_FREE_BYTES = 16 << 20
ENOSPC = "No space left on device"


class PgError(RuntimeError):
    pass


class DiskFull(PgError):
    """The disk holding <appData> is full. The engine reports it and pauses its jobs; once space
    is freed, `ensure_running` starts the server again. Backups are never deleted to make room."""

    def __init__(self, volume: Path, detail: str) -> None:
        self.volume = volume
        self.free_bytes = free_bytes(volume)
        super().__init__(
            f"The disk holding {volume} is full ({self.free_bytes / 2**20:.1f} MB free), so the "
            f"store is paused until space is freed: {detail}"
        )


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
    def socket(self) -> Path:
        return self.pgsocket / f".s.PGSQL.{PORT}"

    @property
    def backups(self) -> Path:
        return self.app_data / "backups"

    @property
    def log(self) -> Path:
        return self.app_data / "logs" / "postgres.log"

    @property
    def lock(self) -> Path:
        return self.app_data / "pg.lock"

    def staging(self, kind: str) -> Path:
        """pgdata.initdb (first launch), pgdata.upgrade and pgdata.upgraded (major-version move)."""
        return self.app_data / f"pgdata.{kind}"

    def tool(self, name: str) -> str:
        return str(self.bin / name)


@dataclass(frozen=True)
class PidFile:
    """postmaster.pid: pid, data dir, start time, port, socket dir, listen address, shmem key,
    status. The status line is `starting`, `ready` or `stopping`."""

    pid: int
    data_dir: str
    port: int
    socket_dir: str
    status: str


@dataclass(frozen=True)
class Server:
    postmaster_pid: int
    action: str  # "initdb+start", "start", "adopt" or "upgrade"
    notes: tuple[str, ...] = ()


# --- small helpers -----------------------------------------------------------------------------


def run(cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(list(cmd), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise PgError(
            f"{Path(cmd[0]).name} failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}"
        )
    return p


def free_bytes(path: Path) -> int:
    """Free space for an unprivileged user on the volume holding `path` (or its nearest parent
    that exists)."""
    while not path.exists() and path != path.parent:
        path = path.parent
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


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
    """The process's executable (`ps -o comm=`); '' if it is gone."""
    return run(["ps", "-o", "comm=", "-p", str(pid)], check=False).stdout.strip()


def is_postgres(pid: int) -> bool:
    return Path(process_command(pid)).name == "postgres"


def wait_gone(pid: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while pid_alive(pid):
        if time.monotonic() > deadline:
            raise PgError(f"pid {pid} still running after {timeout_s}s")
        time.sleep(0.1)


@contextlib.contextmanager
def locked(layout: Layout) -> Generator[None]:
    """Serialise initdb, start, adopt, stop and restore between engines. The kernel drops an
    flock when its holder dies, so a kill -9 never leaves it held."""
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
    """Run SQL through the socket; its output, unaligned and without headers."""
    p = run(
        [
            layout.tool("psql"), "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1",
            "-h", socket_dir or str(layout.pgsocket), "-p", str(port), "-U", USER, "-d", db,
            "-c", sql,
        ]
    )  # fmt: skip
    return p.stdout.strip()


def check_socket_path(layout: Layout) -> None:
    n = len(str(layout.socket).encode())
    if n > MAX_SOCKET_PATH:
        raise PgError(
            f"Can't run the store: its socket path {layout.socket} is {n} bytes, and macOS "
            f"allows {MAX_SOCKET_PATH}. Under /Users/<name>/Library/Application Support that "
            "leaves 33 bytes for the home folder's name."
        )


# --- the server's state ------------------------------------------------------------------------


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
    including another cluster's postgres, and `pg_ctl status` (which only checks the pid)
    reports it as running. It is ours only if the process is a postgres and a connection through
    the socket it advertises reports this data directory.

    Stale is returned only on positive evidence, because the caller then deletes the lock files:
    the pid is dead, the process isn't postgres, the socket answers for another data directory,
    or the socket doesn't exist or refuses connections. Any other failure (too many clients, a
    fork failure, out of memory) can come from our own healthy server, so it raises.
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
    refused = 0
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
        err = p.stderr.strip()
        if "No such file or directory" in err or "Connection refused" in err:
            # No socket, or nothing listening on it: the pid is some other postgres. macOS also
            # refuses when a live server's listen backlog is full, so look again before deciding.
            refused += 1
            if refused >= 5:
                return Stale(pf, f"pid {pf.pid} is a postgres not listening on our socket: {err}")
            time.sleep(0.5)
            continue
        busy = ("starting up", "shutting down", "not yet accepting connections", "recovery")
        if not any(s in err for s in busy):
            raise PgError(f"can't tell whether postgres pid {pf.pid} is ours: {err}")
        if time.monotonic() > deadline:
            raise PgError(f"postgres pid {pf.pid} is still {pf.status or 'busy'} after {wait_s}s")
        time.sleep(0.2)


def _log_since_last_start(layout: Layout) -> str:
    """The postgres log from the server's last start on (the log is appended to at each)."""
    try:
        with open(layout.log, "rb") as f:
            f.seek(max(f.seek(0, os.SEEK_END) - (256 << 10), 0))
            tail = f.read().decode(errors="replace")
    except FileNotFoundError:
        return ""
    i = tail.rfind("starting PostgreSQL")
    return tail[i:] if i >= 0 else tail


def disk_full(layout: Layout) -> DiskFull | None:
    """Whether our server is down because the disk is full (§5.3, Disk): it isn't running, and
    either its log since it last started says so or there isn't room for a WAL segment (on a
    full disk the log itself may not have been written). The engine checks this when it loses
    the store, to report the pause instead of an error."""
    state = inspect_pid_file(layout)
    if isinstance(state, Running):
        return None
    if ENOSPC in _log_since_last_start(layout):
        return DiskFull(layout.app_data, "Postgres stopped")
    if free_bytes(layout.app_data) < MIN_FREE_BYTES:
        return DiskFull(layout.app_data, "Postgres can't write")
    return None


def _disk_full_or(layout: Layout, err: PgError, text: str = "") -> PgError:
    """`err` as DiskFull when it (or `text`, such as the log) shows the disk was full."""
    if ENOSPC in str(err) or ENOSPC in text:
        return DiskFull(layout.app_data, str(err).splitlines()[0])
    return err


# --- lifecycle ---------------------------------------------------------------------------------


def initdb(layout: Layout, target: Path) -> None:
    """initdb into a staging directory (first launch or a major-version move), first discarding
    whatever a crash left there. Local socket connections are trusted and TCP is rejected: the
    0700 socket directory is the access control."""
    discard_staging(layout, target)
    try:
        run(
            [
                layout.tool("initdb"), "-D", str(target), "-E", "UTF8", "--locale=C", "-U", USER,
                "--auth-local=trust", "--auth-host=reject",
            ]
        )  # fmt: skip
    except PgError as e:
        shutil.rmtree(target, ignore_errors=True)
        raise _disk_full_or(layout, e) from e


def discard_staging(layout: Layout, staging: Path) -> None:
    """Delete a staging cluster, stopping its server first if a crash left it running."""
    if not staging.exists():
        return
    state = inspect_pid_file(layout, pgdata=staging)
    if isinstance(state, Running):
        os.kill(state.pid_file.pid, signal.SIGQUIT)  # immediate shutdown
        wait_gone(state.pid_file.pid, STOP_TIMEOUT_S)
    shutil.rmtree(staging)


def first_launch(layout: Layout, prepare_store: Callable[[Layout], None] | None) -> None:
    """§5.3, First launch. All of it happens in pgdata.initdb/: initdb, start, create the store
    and its extensions, `prepare_store` (the Alembic chains), stop. Only then is it renamed to
    pgdata/, so a crash or an error at any step leaves no pgdata/, and the next start runs first
    launch again from the beginning."""
    staging = layout.staging("initdb")
    initdb(layout, staging)
    clear_stale_socket(layout)
    start(layout, staging)
    try:
        psql(layout, f"CREATE DATABASE {DATABASE}", db="postgres")
        psql(layout, "; ".join(f"CREATE EXTENSION {x}" for x in EXTENSIONS))
        if prepare_store:
            prepare_store(layout)
    except BaseException:
        _stop_dir(layout, staging, "immediate")
        shutil.rmtree(staging)
        raise
    _stop_dir(layout, staging, "fast")
    staging.rename(layout.pgdata)


def start(layout: Layout, pgdata: Path | None = None) -> None:
    """pg_ctl start on our socket with §5.3's settings, waiting until it accepts connections
    (WAL replay after a crash included)."""
    check_socket_path(layout)
    layout.pgsocket.mkdir(mode=0o700, parents=True, exist_ok=True)
    layout.pgsocket.chmod(0o700)
    layout.log.parent.mkdir(parents=True, exist_ok=True)
    # pg_ctl hands -o to /bin/sh, so each value is shell-quoted: <appData> has a space in it.
    opts = ["-k", str(layout.pgsocket), "-p", str(PORT)]
    for k, v in SETTINGS.items():
        opts += ["-c", f"{k}={v}"]
    log_offset = layout.log.stat().st_size if layout.log.exists() else 0
    try:
        run(
            [
                layout.tool("pg_ctl"), "start", "-D", str(pgdata or layout.pgdata),
                "-l", str(layout.log), "-w", "-t", str(START_TIMEOUT_S), "-o", shlex.join(opts),
            ]
        )  # fmt: skip
    except PgError as e:
        try:
            with open(layout.log, "rb") as f:
                f.seek(log_offset)
                log = f.read().decode(errors="replace")
        except FileNotFoundError:
            log = ""
        if free_bytes(layout.app_data) < MIN_FREE_BYTES:
            log += ENOSPC
        raise _disk_full_or(layout, e, log) from e


def ensure_running(
    layout: Layout,
    *,
    prepare_store: Callable[[Layout], None] | None = None,
    confirm_stale_dump: Callable[[str], bool] = lambda _: True,
) -> Server:
    """Start or adopt the store's server (§5.3).

    `prepare_store` runs once, at first launch, against the new store before it becomes
    pgdata/: the place for the Alembic chains. `confirm_stale_dump` is asked before a
    major-version move restores a dump that misses later changes; it gets the reason.

    Raises DiskFull when the server can't start for want of space; see `wait_until_running`.
    """
    check_socket_path(layout)  # before anything is written
    with locked(layout):
        notes: list[str] = []
        if resumed := resume_upgrade(layout):
            notes.append(resumed)
        have = data_major(layout)
        if have is None:
            first_launch(layout, prepare_store)
            start(layout)
            return Server(_postmaster_pid(layout), "initdb+start", tuple(notes))
        if have != binary_major(layout):
            return upgrade_major(layout, have, confirm_stale_dump, notes)

        state = inspect_pid_file(layout)
        if isinstance(state, Running):
            if resumed := resume_restore(layout):
                notes.append(resumed)
            return Server(state.pid_file.pid, "adopt", tuple(notes))
        if isinstance(state, Stale):
            # Postgres removes a lock file whose pid is dead, but refuses to start while that
            # pid is alive, whatever the process is.
            notes.append(f"stale postmaster.pid: {state.reason}")
            (layout.pgdata / "postmaster.pid").unlink()
        clear_stale_socket(layout)
        start(layout)
        if resumed := resume_restore(layout):
            notes.append(resumed)
        return Server(_postmaster_pid(layout), "start", tuple(notes))


def wait_until_running(
    layout: Layout,
    *,
    on_disk_full: Callable[[DiskFull], None],
    poll_s: float = 5.0,
    cancel: threading.Event | None = None,
    prepare_store: Callable[[Layout], None] | None = None,
    confirm_stale_dump: Callable[[str], bool] = lambda _: True,
) -> Server:
    """The disk-full pause (§5.3, Disk): `ensure_running`, and while the disk is too full for
    the server to start, report it once through `on_disk_full` (the engine shows it and pauses
    its jobs) and try again every `poll_s` seconds until space is freed. Nothing is deleted to
    make room. If `cancel` is set while waiting, the DiskFull is raised."""
    reported = False
    while True:
        try:
            return ensure_running(
                layout, prepare_store=prepare_store, confirm_stale_dump=confirm_stale_dump
            )
        except DiskFull as e:
            if not reported:
                on_disk_full(e)
                reported = True
            if cancel is None:
                time.sleep(poll_s)
            elif cancel.wait(poll_s):
                raise


def stop(layout: Layout, *, quit_dump: bool = True) -> Path | None:
    """Engine shutdown (§5.3, Stop): the quit dump, then a fast stop. Returns the dump, or None
    if no server of ours was running. If the dump fails (a full disk, say), the server is still
    stopped, and then the dump's error is raised."""
    with locked(layout):
        state = inspect_pid_file(layout)
        if not isinstance(state, Running):
            return None
        path: Path | None = None
        failed: PgError | None = None
        if quit_dump:
            try:
                path = dump(layout, "quit")
            except PgError as e:
                failed = e
        _stop_dir(layout, layout.pgdata, "fast")
        if failed:
            raise failed
        return path


def _postmaster_pid(layout: Layout) -> int:
    pf = read_pid_file(layout.pgdata)
    if pf is None:
        raise PgError("the server started but wrote no postmaster.pid")
    return pf.pid


def _stop_dir(layout: Layout, pgdata: Path, mode: str) -> None:
    run(
        [layout.tool("pg_ctl"), "stop", "-D", str(pgdata), "-m", mode, "-w",
         "-t", str(STOP_TIMEOUT_S)]
    )  # fmt: skip


def clear_stale_socket(layout: Layout) -> None:
    """Remove the socket and its lock file (`.s.PGSQL.<port>.lock`). Call only when no server of
    ours is running: the socket directory is ours alone, so they are left from a crash. The
    socket lock holds the postmaster's pid and Postgres checks it the way it checks
    postmaster.pid, so a reused pid there blocks the start too."""
    for f in layout.pgsocket.glob(".s.PGSQL.*"):
        f.unlink(missing_ok=True)


# --- dumps and restore -------------------------------------------------------------------------


def dump(layout: Layout, kind: str, *, socket_dir: str | None = None, port: int = PORT) -> Path:
    """pg_dump -Fc of the store into backups/ (§5.3, Dumps). `kind` is premigrate, compile or
    quit. Written under a temporary name and renamed when complete, so a crash or a full disk
    mid-dump never leaves a truncated file that looks like the newest dump. Then the oldest are
    pruned to the newest KEEP_DUMPS, never including the dump just written."""
    layout.backups.mkdir(parents=True, exist_ok=True)
    # One UTC clock read: names sort in the order they were written across time-zone and DST
    # changes, and newest_dump() and pruning go by name.
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    final = layout.backups / f"ontology-{ts}-{kind}.dump"
    part = final.with_name(final.name + ".part")
    try:
        run(
            [
                layout.tool("pg_dump"), "-Fc", "-h", socket_dir or str(layout.pgsocket),
                "-p", str(port), "-U", USER, "-d", DATABASE, "-f", str(part),
            ]
        )  # fmt: skip
    except PgError as e:
        part.unlink(missing_ok=True)
        raise _disk_full_or(layout, e) from e
    part.rename(final)
    for old in sorted(layout.backups.glob("ontology-*.dump"))[:-KEEP_DUMPS]:
        if old != final:  # if the clock was set back, the new name can sort first
            old.unlink()
    return final


def newest_dump(layout: Layout) -> Path | None:
    dumps = sorted(layout.backups.glob("ontology-*.dump"))
    return dumps[-1] if dumps else None


def restore(layout: Layout, path: Path) -> None:
    """Put the store back to a dump (§5.3, Migrations), with the server running.

    The dump goes into a new database, RESTORE_DATABASE, which replaces the store only once
    pg_restore has succeeded: drop the store, then rename the new one. Until then the store is
    untouched, so a failed pg_restore or a crash leaves it as it was. A crash between the drop
    and the rename leaves only RESTORE_DATABASE, complete, and the next start renames it
    (`resume_restore`). `pg_restore --clean` into the store won't do: it only drops objects that
    are in the dump, so a table a failed migration created survives it.

    `DROP DATABASE … WITH (FORCE)` ends every connection to the store, so the engine's pool has
    to reconnect afterwards."""
    if not path.is_file():
        raise PgError(f"the dump {path} is missing; the store is unchanged")
    with locked(layout):  # resume_restore() runs under the same lock
        psql(layout, f"DROP DATABASE IF EXISTS {RESTORE_DATABASE} WITH (FORCE)", db="postgres")
        psql(layout, f"CREATE DATABASE {RESTORE_DATABASE}", db="postgres")
        try:
            run(
                [
                    layout.tool("pg_restore"), "--exit-on-error", "--single-transaction",
                    "-h", str(layout.pgsocket), "-p", str(PORT), "-U", USER,
                    "-d", RESTORE_DATABASE, str(path),
                ]
            )  # fmt: skip
        except PgError as e:
            psql(layout, f"DROP DATABASE {RESTORE_DATABASE} WITH (FORCE)", db="postgres")
            msg = f"restoring {path.name} failed; the store is unchanged: {e}"
            if ENOSPC in str(e):
                raise DiskFull(layout.app_data, msg) from e
            raise PgError(msg) from e
        # DROP DATABASE can't run inside a transaction, so this takes two statements.
        psql(layout, f"DROP DATABASE {DATABASE} WITH (FORCE)", db="postgres")
        psql(layout, f"ALTER DATABASE {RESTORE_DATABASE} RENAME TO {DATABASE}", db="postgres")


def resume_restore(layout: Layout) -> str | None:
    """Finish or discard a restore() that a crash interrupted. Call with the server running."""
    dbs = set(psql(layout, "SELECT datname FROM pg_database", db="postgres").split())
    if RESTORE_DATABASE not in dbs:
        return None
    if DATABASE in dbs:
        # The crash came before the store was dropped: the store is as it was. Discard.
        psql(layout, f"DROP DATABASE {RESTORE_DATABASE} WITH (FORCE)", db="postgres")
        return "discarded an unfinished restore"
    psql(layout, f"ALTER DATABASE {RESTORE_DATABASE} RENAME TO {DATABASE}", db="postgres")
    return "finished an interrupted restore"


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


def resume_upgrade(layout: Layout) -> str | None:
    """Finish or discard a major-version move that a crash interrupted (see upgrade_major).

    pgdata.upgraded/ is a complete, stopped cluster. If pgdata/ is missing, the crash came after
    the old cluster was moved aside, so finish the swap. If pgdata/ is still the old cluster,
    delete pgdata.upgraded/ and let the move run again from the same dump. pgdata.upgrade/ is a
    staging cluster that never finished restoring: delete it too."""
    done = layout.staging("upgraded")
    if done.exists():
        if not layout.pgdata.exists():
            done.rename(layout.pgdata)
            return "finished an interrupted major-version move"
        shutil.rmtree(done)
        return "discarded a major-version cluster that was never swapped in"
    staging = layout.staging("upgrade")
    if staging.exists():
        discard_staging(layout, staging)
        return "discarded an unfinished major-version staging cluster"
    return None


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
        wait_gone(pf.pid, STOP_TIMEOUT_S)
        notes.append(f"dumped the running PostgreSQL {old} server live, then stopped it")
    path = newest_dump(layout)
    if path is None:
        raise PgError(f"the store is PostgreSQL {old} data and there is no dump to restore")
    ok, why = dump_is_current(layout.pgdata, path)
    if not ok:
        notes.append(f"stale dump: {why}")
        if not confirm_stale_dump(why):
            raise PgError(f"upgrade cancelled: {why}")
    # Build and fill the new cluster beside the old one; pgdata/ stays the old cluster until the
    # restore has succeeded. A crash or a failed restore before the swap leaves pgdata/ as it
    # was, so the next start tries the move again instead of finding no pgdata/ and running a
    # first launch onto an empty store.
    staging = layout.staging("upgrade")
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
    except BaseException as e:
        _stop_dir(layout, staging, "immediate")
        shutil.rmtree(staging)
        if isinstance(e, PgError):
            raise _disk_full_or(layout, e) from e
        raise
    _stop_dir(layout, staging, "fast")
    # Two renames. pgdata.upgraded/ marks the staging cluster complete, so resume_upgrade can
    # finish the swap if a crash lands between moving the old one aside and moving this in.
    done = staging.rename(layout.staging("upgraded"))
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    aside = layout.pgdata.rename(layout.app_data / f"pgdata-{old}-{ts}")
    done.rename(layout.pgdata)
    start(layout)
    notes += [f"restored {path.name}", f"old data kept in {aside.name}"]
    return Server(_postmaster_pid(layout), "upgrade", tuple(notes))
