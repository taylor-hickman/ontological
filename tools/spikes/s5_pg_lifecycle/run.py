#!/usr/bin/env python3
"""Spike S5: does the bundled Postgres lifecycle in ARCHITECTURE.md §5.3 work?

Runs the prototype `pg.py` against the S4 binaries through every lifecycle case in §12's
"Postgres lifecycle tests", and prints `ok`/`FAIL` per check and a summary per case:

  first-launch     initdb, start, the §5.3 settings, the store database and extensions
  normal-stop      SIGTERM to the engine: quit dump, fast stop, next start needs no recovery
  kill9-adopt      kill -9 of the engine's process group; the server lives on and the next
                   engine adopts it through postmaster.pid with no data lost
  crash-recover    kill -9 of the engine and the postmaster; the next start replays WAL
  stale-pid        postmaster.pid (and the socket lock) name a live pid that is not our
                   server: an unrelated process, then another cluster's postmaster
  second-engine    a second engine while one runs, and three engines first-launching at once
  failed-migration a revision fails after an earlier one committed; the pre-migration dump
                   is restored while the engine holds a connection open
  major-version    a PostgreSQL 16 store (made by pg.py on 16 binaries) moved to 17: from a
                   clean quit dump, from a stale dump after a crash, and from a 16 server
                   still running; then the engine killed mid-restore and mid-swap, and a
                   restore that fails, none of which may leave pgdata/ missing or empty

Each engine is `pg.py` run as a subprocess in its own session, so it can be killed like the
real one. Every <appData> has a space in its path, like ~/Library/Application Support.

Usage:
  python3 tools/spikes/s5_pg_lifecycle/run.py <pg17 bin> <pg16 bin> [--only CASE ...] [--keep]

<pg17 bin> is the bin/ of a signed trim.sh tree (tools/spikes/s4_postgres). <pg16 bin> is any
PostgreSQL 16 build with pgvector. This makes one in <work>/postgres/bin:

  VERSIONS_ENV=tools/spikes/s5_pg_lifecycle/pg16.env tools/spikes/s4_postgres/build.sh <work>
"""

from __future__ import annotations

import argparse
import contextlib
import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pg  # noqa: E402

PG_PY = HERE / "pg.py"
# Every engine process started, so a failed case doesn't leave one running.
PROCS: list[subprocess.Popen[str]] = []


def spawn_engine(layout: pg.Layout) -> subprocess.Popen[str]:
    p = subprocess.Popen(
        [sys.executable, str(PG_PY), str(layout.app_data), str(layout.bin)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    PROCS.append(p)
    return p


class CaseFailed(Exception):
    pass


def check(desc: str, cond: bool, detail: object = "") -> None:
    if not cond:
        raise CaseFailed(f"{desc}: {detail}")
    print(f"  ok - {desc}")


class Engine:
    """pg.py as a stand-in engine process."""

    def __init__(self, layout: pg.Layout) -> None:
        self.layout = layout
        self.proc = spawn_engine(layout)
        line = self.proc.stdout.readline() if self.proc.stdout else ""
        if not line:
            self.proc.wait(10)
            raise CaseFailed(f"engine exited {self.proc.returncode} before it was ready")
        self.info: dict[str, object] = json.loads(line)
        print(f"  engine {self.proc.pid}: {self.info}")

    @property
    def action(self) -> object:
        return self.info["action"]

    @property
    def postmaster(self) -> int:
        return int(str(self.info["postmaster_pid"]))

    def kill9(self) -> None:
        """kill -9 of the engine's whole process group, as a crash or a force quit does."""
        os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait(10)

    def term(self) -> dict[str, object]:
        self.proc.send_signal(signal.SIGTERM)
        line = self.proc.stdout.readline() if self.proc.stdout else ""
        self.proc.wait(60)
        if self.proc.returncode != 0 or not line:
            raise CaseFailed(f"engine stop exited {self.proc.returncode}")
        return json.loads(line)


class Ctx:
    def __init__(self, root: Path, bin17: Path, bin16: Path) -> None:
        self.root, self.bin17, self.bin16 = root, bin17, bin16
        self.layouts: list[pg.Layout] = []
        self.decoys: list[subprocess.Popen[bytes]] = []

    def layout(self, name: str, bin: Path | None = None) -> pg.Layout:
        # Short root: the socket path has to fit in 103 bytes.
        lay = pg.Layout(self.root / name / "App Support" / "Ontological", bin or self.bin17)
        self.layouts.append(lay)
        return lay

    def cleanup(self) -> None:
        for p in PROCS:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
        for d in self.decoys:
            d.kill()
            d.wait()
        for lay in self.layouts:
            pf = pg.read_pid_file(lay.pgdata)
            if pf and pg.pid_alive(pf.pid) and Path(pg.process_command(pf.pid)).name == "postgres":
                os.kill(pf.pid, signal.SIGQUIT)  # immediate shutdown
                pg._wait_gone(pf.pid, 30)


# --- helpers -----------------------------------------------------------------------------------


def sql(lay: pg.Layout, q: str, db: str = pg.DATABASE) -> str:
    return pg.psql(lay, q, db=db)


def seed(lay: pg.Layout, start: int, n: int) -> None:
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


def wait_gone(pid: int) -> None:
    pg._wait_gone(pid, 30)


def crash_postmaster(lay: pg.Layout, pid: int) -> None:
    """kill -9 the postmaster and wait for its children to notice and exit. Until they do they
    keep the shared memory attached, and Postgres refuses to start on top of them."""
    os.kill(pid, signal.SIGKILL)
    wait_gone(pid)
    deadline = time.monotonic() + 30
    while any("postgres: " in c for c in _children_of_dead(lay)):
        if time.monotonic() > deadline:
            raise CaseFailed("postgres children still running 30 s after the postmaster died")
        time.sleep(0.1)


def _children_of_dead(lay: pg.Layout) -> list[str]:
    # Backends' titles don't name the data dir; on a test machine any orphaned postgres child
    # (parent pid 1) left over from our kill is ours.
    out = subprocess.run(["ps", "-axo", "ppid=,command="], capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if ln.strip().startswith("1 ") and "postgres: " in ln]


def postmasters_for(lay: pg.Layout) -> list[str]:
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if f"-D {lay.pgdata}" in ln and "pg_ctl" not in ln]


def log_since(lay: pg.Layout, offset: int) -> str:
    with open(lay.log, "rb") as f:
        f.seek(offset)
        return f.read().decode(errors="replace")


def log_size(lay: pg.Layout) -> int:
    return lay.log.stat().st_size if lay.log.exists() else 0


def forge_pid(lay: pg.Layout, pid: int) -> None:
    """Point postmaster.pid and the socket lock at `pid`, as after a reboot that reused it."""
    files = [lay.pgdata / "postmaster.pid", lay.pgsocket / f".s.PGSQL.{pg.PORT}.lock"]
    for f in files:
        lines = f.read_text().splitlines()
        lines[0] = str(pid)
        f.write_text("\n".join(lines) + "\n")


# --- cases -------------------------------------------------------------------------------------


def case_first_launch(ctx: Ctx) -> None:
    lay = ctx.layout("first")
    e = Engine(lay)
    check("action is initdb+start", e.action == "initdb+start", e.action)
    check("PG_VERSION is 17", pg.data_major(lay) == 17, pg.data_major(lay))
    settings = sql(
        lay,
        "SELECT string_agg(s, ' ' ORDER BY s) FROM ("
        "SELECT name || '=' || setting AS s FROM pg_settings WHERE name IN "
        "('listen_addresses','max_connections','shared_buffers','unix_socket_directories',"
        "'server_encoding') UNION ALL SELECT 'lc_collate=' || datcollate FROM pg_database "
        f"WHERE datname = '{pg.DATABASE}') t",
    )
    want = (
        f"lc_collate=C listen_addresses= max_connections=30 server_encoding=UTF8 "
        f"shared_buffers=16384 unix_socket_directories={lay.pgsocket}"
    )
    check(
        "settings: UTF8, C, no TCP, 128MB, 30 connections, socket dir", settings == want, settings
    )
    mode = lay.pgsocket.stat().st_mode & 0o777
    check("pgsocket is 0700", mode == 0o700, oct(mode))
    hba = sql(
        lay,
        "SELECT string_agg(type || ':' || auth_method, ',' ORDER BY line_number) "
        "FROM pg_hba_file_rules",
    )
    check(
        "pg_hba: trust on the socket, every host line rejects",
        all(r in ("local:trust",) or r.endswith(":reject") for r in hba.split(","))
        and "local:trust" in hba,
        hba,
    )
    exts = sql(lay, "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension")
    check("store database has vector and pg_trgm", exts == "pg_trgm,plpgsql,vector", exts)
    seed(lay, 1, 100)
    check("HNSW query works", nearest(lay) != "", "")
    e.term()


def case_normal_stop(ctx: Ctx) -> None:
    lay = ctx.layout("stop")
    a = Engine(lay)
    seed(lay, 1, 500)
    out = a.term()
    dump = lay.backups / str(out["quit_dump"])
    check("SIGTERM wrote a quit dump", dump.exists() and dump.name.endswith("-quit.dump"), out)
    listing = pg.run([lay.tool("pg_restore"), "-l", str(dump)]).stdout
    check("quit dump is readable and has items", "TABLE DATA public items" in listing, "")
    wait_gone(a.postmaster)
    check("postmaster exited", not pg.pid_alive(a.postmaster), a.postmaster)
    check("postmaster.pid removed", not (lay.pgdata / "postmaster.pid").exists(), "")
    check("log says shut down", "database system is shut down" in lay.log.read_text(), "")
    off = log_size(lay)
    b = Engine(lay)
    check("next engine starts it (no initdb, no adopt)", b.action == "start", b.action)
    log = log_since(lay, off)
    check(
        "no crash recovery on restart",
        "was interrupted" not in log and "redo starts" not in log,
        log,
    )
    check("rows intact", count(lay) == 500, count(lay))
    b.term()


def case_kill9_adopt(ctx: Ctx) -> None:
    lay = ctx.layout("adopt")
    a = Engine(lay)
    seed(lay, 1, 500)
    a.kill9()
    time.sleep(0.5)
    check(
        "postmaster survives kill -9 of the engine's process group", pg.pid_alive(a.postmaster), ""
    )
    off = log_size(lay)
    b = Engine(lay)
    check("next engine adopts", b.action == "adopt", b.action)
    check("same postmaster", b.postmaster == a.postmaster, (a.postmaster, b.postmaster))
    check("adopt took under 1 s", float(str(b.info["seconds"])) < 1, b.info["seconds"])
    check("server was not restarted", "database system is ready" not in log_since(lay, off), "")
    check("rows intact", count(lay) == 500, count(lay))
    seed(lay, 501, 10)
    b.term()
    check("adopting engine stops it cleanly", not (lay.pgdata / "postmaster.pid").exists(), "")


def case_crash_recover(ctx: Ctx) -> None:
    lay = ctx.layout("crash")
    a = Engine(lay)
    seed(lay, 1, 500)
    a.kill9()
    crash_postmaster(lay, a.postmaster)
    check("postmaster.pid left behind", (lay.pgdata / "postmaster.pid").exists(), "")
    off = log_size(lay)
    b = Engine(lay)
    check(
        "next engine starts a new server",
        b.action == "start" and b.postmaster != a.postmaster,
        b.info,
    )
    log = log_since(lay, off)
    check("WAL replayed", "was interrupted" in log and "redo done" in log, log)
    check("committed rows survive", count(lay) == 500, count(lay))
    b.term()


def case_stale_pid(ctx: Ctx) -> None:
    # (a) the pid now belongs to an unrelated process of the same user
    lay = ctx.layout("stale")
    a = Engine(lay)
    seed(lay, 1, 500)
    a.kill9()
    crash_postmaster(lay, a.postmaster)
    decoy = subprocess.Popen(["sleep", "600"])
    ctx.decoys.append(decoy)
    forge_pid(lay, decoy.pid)
    st = pg.run([lay.tool("pg_ctl"), "status", "-D", str(lay.pgdata)], check=False)
    check(
        "pg_ctl status is fooled by the reused pid",
        st.returncode == 0 and "server is running" in st.stdout,
        st.stdout,
    )
    raw = pg.run(
        [lay.tool("pg_ctl"), "start", "-D", str(lay.pgdata), "-l", str(lay.log), "-w", "-t", "10",
         "-o", f"-k '{lay.pgsocket}' -c listen_addresses=''"], check=False)  # fmt: skip
    log = lay.log.read_text()
    check(
        "a plain pg_ctl start refuses: lock file names a live pid",
        raw.returncode != 0 and 'lock file "postmaster.pid" already exists' in log,
        raw.stderr,
    )
    (lay.pgdata / "postmaster.pid").unlink()
    off = log_size(lay)
    raw = pg.run(raw.args, check=False)
    check(
        "without postmaster.pid it still refuses: the socket lock names the same pid",
        raw.returncode != 0 and f'.s.PGSQL.{pg.PORT}.lock" already exists' in log_since(lay, off),
        raw.stderr,
    )
    b = Engine(lay)
    notes = " ".join(map(str, b.info["notes"]))  # type: ignore[arg-type]
    check("engine clears the stale socket lock and starts", b.action == "start", b.info)
    check("decoy untouched", decoy.poll() is None, "")
    check("rows intact after recovery", count(lay) == 500, count(lay))
    b.term()

    # (a') the same, with postmaster.pid still in place
    a = Engine(lay)
    a.kill9()
    crash_postmaster(lay, a.postmaster)
    forge_pid(lay, decoy.pid)
    b = Engine(lay)
    notes = " ".join(map(str, b.info["notes"]))  # type: ignore[arg-type]
    check(
        "engine sees the pid is not postgres and starts",
        b.action == "start" and "not postgres" in notes,
        b.info,
    )
    check("rows intact", count(lay) == 500, count(lay))
    b.term()

    # (b) the pid is another cluster's postmaster
    other = ctx.layout("stale-other")
    o = Engine(other)
    c = Engine(lay)
    c.kill9()
    crash_postmaster(lay, c.postmaster)
    forge_pid(lay, o.postmaster)
    d = Engine(lay)
    notes = " ".join(map(str, d.info["notes"]))  # type: ignore[arg-type]
    check(
        "another cluster's postmaster is not adopted",
        d.action == "start" and d.postmaster != o.postmaster,
        d.info,
    )
    check("reason given", "not this server" in notes or "serves" in notes, notes)
    check("other cluster still running", pg.pid_alive(o.postmaster), "")
    check("rows intact", count(lay) == 500, count(lay))
    d.term()
    o.term()


def case_second_engine(ctx: Ctx) -> None:
    lay = ctx.layout("second")
    a = Engine(lay)
    b = Engine(lay)
    check(
        "second engine adopts the first's server",
        b.action == "adopt" and b.postmaster == a.postmaster,
        b.info,
    )
    check("one postmaster for the data dir", len(postmasters_for(lay)) == 1, postmasters_for(lay))
    b.kill9()
    check("killing the second engine leaves the server up", pg.pid_alive(a.postmaster), "")
    a.term()

    # Three engines first-launching at once on an empty <appData>.
    lay = ctx.layout("race")
    procs = [spawn_engine(lay) for _ in range(3)]
    infos = [json.loads(p.stdout.readline()) for p in procs if p.stdout]  # type: ignore[union-attr]
    actions = sorted(i["action"] for i in infos)
    check("one initdb, the others adopt", actions == ["adopt", "adopt", "initdb+start"], actions)
    check("all on one postmaster", len({i["postmaster_pid"] for i in infos}) == 1, infos)
    check("one postmaster for the data dir", len(postmasters_for(lay)) == 1, postmasters_for(lay))
    for p in procs[1:]:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
    procs[0].send_signal(signal.SIGTERM)
    procs[0].wait(60)

    # Why pg.py takes <appData>/pg.lock: the same race without it.
    lay = ctx.layout("race-nolock")
    with mp.get_context("fork").Pool(3) as pool:
        results = pool.map(_ensure_running_unlocked, [lay] * 3)
    errors = [r for r in results if r.startswith("error")]
    print(f"  without the lock: {results}")
    check("without the lock, concurrent first launches fail in initdb", errors != [], results)


def _ensure_running_unlocked(lay: pg.Layout) -> str:
    pg.locked = lambda _layout: contextlib.nullcontext()  # type: ignore[assignment]
    try:
        return pg.ensure_running(lay).action
    except pg.PgError as e:
        return f"error: {str(e).splitlines()[0][:120]}"


REV1 = (
    "CREATE SCHEMA app; CREATE TABLE app.setting (key text PRIMARY KEY, value jsonb);"
    "INSERT INTO app.setting VALUES ('theme', '\"dark\"')"
)
REV2 = "CREATE TABLE app.conversation (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), title text)"
REV3 = "ALTER TABLE app.setting ADD COLUMN updated timestamptz; SELECT 1 / 0"


def case_failed_migration(ctx: Ctx) -> None:
    lay = ctx.layout("migrate")
    e = Engine(lay)
    check("revision 1 applies", pg.migrate(lay, [REV1]) == 1, "")
    seed(lay, 1, 500)
    before = nearest(lay)
    # The engine's pool holds connections while migrations run.
    held = subprocess.Popen([lay.tool("psql"), "-X", "-h", str(lay.pgsocket), "-p", str(pg.PORT),
                             "-U", pg.USER,
                             "-d", pg.DATABASE, "-c", "SELECT pg_sleep(60)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    time.sleep(0.3)
    try:
        pg.migrate(lay, [REV1, REV2, REV3])
        raise CaseFailed("revision 3 should have failed")
    except pg.MigrationFailed as failed:
        err = failed
    check("revision 3 fails after revision 2 committed", err.rev == 3, err)
    check("pre-migration dump kept", err.restored_from.exists(), err.restored_from)
    held.wait(10)
    check("restore closed the held connection", held.returncode != 0, held.returncode)
    check("version back to 1", sql(lay, "SELECT rev FROM ops.version") == "1", "")
    check("revision 2's table gone", sql(lay, "SELECT to_regclass('app.conversation')") == "", "")
    cols = sql(
        lay,
        "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) "
        "FROM information_schema.columns WHERE table_schema='app' AND table_name='setting'",
    )
    check("revision 3's column gone", cols == "key,value", cols)
    check(
        "data intact",
        count(lay) == 500 and sql(lay, "SELECT value FROM app.setting") == '"dark"',
        "",
    )
    check("HNSW index restored", nearest(lay) == before, (before, nearest(lay)))

    # Why restore() recreates the database: pg_restore --clean only drops what the dump has.
    sql(lay, REV2)
    clean = pg.run([lay.tool("pg_restore"), "--clean", "--if-exists", "-h", str(lay.pgsocket), "-p",
                    str(pg.PORT), "-U", pg.USER, "-d", pg.DATABASE, str(err.restored_from)],
                   check=False)  # fmt: skip
    check(
        "pg_restore --clean instead fails (DROP SCHEMA app: other objects depend on it)",
        clean.returncode != 0 and "cannot drop schema app" in clean.stderr,
        clean.stderr,
    )
    check(
        "... and leaves revision 2's table behind",
        sql(lay, "SELECT to_regclass('app.conversation')") == "app.conversation",
        "",
    )
    e.term()


def _old_app(ctx: Ctx, name: str) -> tuple[pg.Layout, pg.Layout]:
    """A store made by the previous release (PostgreSQL 16), and the new release's layout."""
    old = ctx.layout(name, ctx.bin16)
    new = pg.Layout(old.app_data, ctx.bin17)
    return old, new


def _check_upgraded(lay: pg.Layout, e: Engine, rows: int, action: str = "upgrade") -> None:
    check("engine moved the store to 17", e.action == action and pg.data_major(lay) == 17, e.info)
    left = sorted(p.name for p in lay.app_data.glob("pgdata.*"))
    check("no staging cluster left", left == [], left)
    check("server is 17", sql(lay, "SHOW server_version_num")[:2] == "17", "")
    check(f"{rows} rows restored", count(lay) == rows, count(lay))
    check(
        "vector restored",
        sql(lay, "SELECT extversion FROM pg_extension WHERE extname='vector'") != "",
        "",
    )
    plan = sql(
        lay,
        "SET enable_seqscan = off;"
        "EXPLAIN SELECT id FROM items ORDER BY embedding <=> '[0,1,3]' LIMIT 1",
    )
    check("HNSW index restored and used", "items_hnsw" in plan, plan)
    asides = list(lay.app_data.glob("pgdata-16-*"))
    check(
        "old data directory kept aside",
        len(asides) == 1 and (asides[0] / "PG_VERSION").read_text().strip() == "16",
        asides,
    )


def case_major_version(ctx: Ctx) -> None:
    # (a) clean: the 16 release quit normally, so its quit dump is current
    old, new = _old_app(ctx, "major")
    a = Engine(old)
    check("old release runs PostgreSQL 16", pg.data_major(old) == 16, pg.data_major(old))
    pg.migrate(old, [REV1])
    seed(old, 1, 500)
    out = a.term()
    check(
        "old release wrote a quit dump with pg_dump 16",
        str(out["quit_dump"]).endswith("-quit.dump"),
        out,
    )
    b = Engine(new)
    _check_upgraded(new, b, 500)
    check(
        "no stale-dump warning",
        not any("stale" in str(n) for n in b.info["notes"]),
        b.info["notes"],
    )  # type: ignore[union-attr]
    check("app schema restored", sql(new, "SELECT rev FROM ops.version") == "1", "")
    b.term()
    c = Engine(new)
    check("next start is a plain start", c.action == "start", c.action)
    c.term()

    # (b) stale: the 16 release crashed after more writes; the newest dump misses them
    old, new = _old_app(ctx, "major-stale")
    a = Engine(old)
    seed(old, 1, 500)
    a.term()
    a = Engine(old)
    seed(old, 501, 100)
    a.kill9()
    crash_postmaster(old, a.postmaster)
    b = Engine(new)
    notes = " ".join(map(str, b.info["notes"]))  # type: ignore[arg-type]
    check(
        "upgrade says the dump is stale",
        "stale dump" in notes and "did not stop cleanly" in notes,
        notes,
    )
    _check_upgraded(new, b, 500)
    b.term()

    # (c) live: the 16 server is still running (its engine was killed, then the app updated)
    old, new = _old_app(ctx, "major-live")
    a = Engine(old)
    seed(old, 1, 500)
    a.term()
    a = Engine(old)
    seed(old, 501, 100)
    a.kill9()
    check("16 server still running", pg.pid_alive(a.postmaster), "")
    b = Engine(new)
    notes = " ".join(map(str, b.info["notes"]))  # type: ignore[arg-type]
    check("upgrade dumped the live 16 server", "dumped the running PostgreSQL 16" in notes, notes)
    check("16 server stopped", not pg.pid_alive(a.postmaster), "")
    _check_upgraded(new, b, 600)
    b.term()

    # (d) the engine dies during the move. pgdata/ must never end up missing or empty: the
    # next start finishes or redoes the move, and never runs a first launch.
    old, new = _old_store(ctx, "major-crash-restore")
    code = _upgrade_until(new, "pg_restore")
    check("engine killed during pg_restore into the staging cluster", code == 9, code)
    check("pgdata/ is still the 16 cluster", pg.data_major(new) == 16, pg.data_major(new))
    b = Engine(new)
    notes = " ".join(map(str, b.info["notes"]))  # type: ignore[arg-type]
    check("next start discards the staging cluster", "discarded an unfinished" in notes, notes)
    _check_upgraded(new, b, 500)
    b.term()

    old, new = _old_store(ctx, "major-crash-swap")
    code = _upgrade_until(new, "aside")
    check("engine killed after moving the 16 cluster aside", code == 9, code)
    check("pgdata/ is missing", not new.pgdata.exists(), "")
    b = Engine(new)
    notes = " ".join(map(str, b.info["notes"]))  # type: ignore[arg-type]
    check("next start finishes the swap, no initdb", "finished an interrupted" in notes, notes)
    _check_upgraded(new, b, 500, action="start")
    b.term()

    # (e) the restore fails: the move stops with an error and pgdata/ stays the 16 cluster
    old, new = _old_store(ctx, "major-bad-dump")
    good = pg.newest_dump(new)
    assert good is not None
    bad = good.with_name(good.name.replace("-quit", "9-quit"))  # sorts after the good one
    bad.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    try:
        Engine(new)
        raise CaseFailed("the upgrade should have failed on a truncated dump")
    except CaseFailed as e:
        check("upgrade fails on a truncated dump", "before it was ready" in str(e), e)
    check("pgdata/ is still the 16 cluster", pg.data_major(new) == 16, pg.data_major(new))
    left = sorted(p.name for p in new.app_data.glob("pgdata*"))
    check("no staging cluster, nothing moved aside", left == ["pgdata"], left)
    bad.unlink()
    b = Engine(new)
    _check_upgraded(new, b, 500)
    b.term()


def _old_store(ctx: Ctx, name: str) -> tuple[pg.Layout, pg.Layout]:
    """A 16 store with 500 rows that quit cleanly."""
    old, new = _old_app(ctx, name)
    a = Engine(old)
    seed(old, 1, 500)
    a.term()
    return old, new


def _upgrade_until(lay: pg.Layout, step: str) -> int | None:
    """Run the 17 engine's upgrade in a forked child that dies (exit 9, like kill -9) at `step`:
    "pg_restore" kills it as pg_restore starts, "aside" right after the old cluster is moved
    aside. Returns the child's exit code."""
    proc = mp.get_context("fork").Process(target=_die_at, args=(lay, step))
    proc.start()
    proc.join(120)
    return proc.exitcode


def _die_at(lay: pg.Layout, step: str) -> None:
    if step == "pg_restore":
        real_run = pg.run

        def run(cmd: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            if Path(cmd[0]).name == "pg_restore":
                os._exit(9)
            return real_run(cmd, **kw)

        pg.run = run  # type: ignore[assignment]
    else:
        real_rename = Path.rename

        def rename(self: Path, target: Any) -> Path:
            moved = real_rename(self, target)
            if Path(target).name.startswith("pgdata-16-"):
                os._exit(9)
            return moved

        Path.rename = rename  # type: ignore[method-assign]
    pg.ensure_running(lay)


CASES: dict[str, Callable[[Ctx], None]] = {
    "first-launch": case_first_launch,
    "normal-stop": case_normal_stop,
    "kill9-adopt": case_kill9_adopt,
    "crash-recover": case_crash_recover,
    "stale-pid": case_stale_pid,
    "second-engine": case_second_engine,
    "failed-migration": case_failed_migration,
    "major-version": case_major_version,
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("bin17", type=Path)
    ap.add_argument("bin16", type=Path)
    ap.add_argument("--only", nargs="+", choices=list(CASES))
    ap.add_argument("--keep", action="store_true", help="keep the temporary <appData> trees")
    args = ap.parse_args()
    bin17, bin16 = args.bin17.resolve(), args.bin16.resolve()
    for b, major in ((bin17, 17), (bin16, 16)):
        got = pg.binary_major(pg.Layout(Path("/nonexistent"), b))
        if got != major:
            ap.error(f"{b} is PostgreSQL {got}, expected {major}")

    root = Path(tempfile.mkdtemp(prefix="s5-", dir="/tmp"))  # short: socket paths max 103 bytes
    ctx = Ctx(root, bin17, bin16)
    results: dict[str, tuple[str, float]] = {}
    try:
        for name, fn in CASES.items():
            if args.only and name not in args.only:
                continue
            print(f"# {name}")
            t0 = time.monotonic()
            try:
                fn(ctx)
                results[name] = ("pass", time.monotonic() - t0)
            except Exception as e:
                print(f"  FAIL - {e}")
                if not isinstance(e, CaseFailed):
                    traceback.print_exc()
                results[name] = ("FAIL", time.monotonic() - t0)
    finally:
        ctx.cleanup()
        if args.keep:
            print(f"kept {root}")
        else:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    print("\n| Case | Result | Seconds |\n|---|---|---:|")
    for name, (res, secs) in results.items():
        print(f"| {name} | {res} | {secs:.1f} |")
    return 0 if all(r == "pass" for r, _ in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
