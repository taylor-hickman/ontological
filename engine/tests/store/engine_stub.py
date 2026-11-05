"""A stand-in engine for the lifecycle tests: runs ontological.store.pg the way the engine will,
as a process the tests can kill -9.

    engine_stub.py <appData> <bin> [--chains] [--prepare-sql SQL] [--restore DUMP | --migrate]
                   [--die-at STEP]

ensure_running(), then one JSON line: {"postmaster_pid", "action", "notes", "seconds"}. Then it
waits; SIGTERM or SIGINT stops the server with a quit dump and prints {"quit_dump": name}. An
error prints {"error", "type"} and exits 1.

--chains runs both Alembic chains during first launch, as the engine does; without it the store
is left unmigrated. --prepare-sql runs SQL in the store during first launch, after the chains.
--restore restores DUMP once the server is up, then exits. --migrate runs `migrate` once the
server is up, prints {"migrated": <the premigrate dump's name, or null>} and exits. --die-at exits
with code 9, as kill -9 would, at one step:

    prepare-store   while first launch prepares the store in pgdata.initdb/
    rename-initdb   right after pgdata.initdb/ becomes pgdata/
    pg_restore      as pg_restore starts (a restore, or a major-version move)
    drop-store      right after restore drops the store, before the rename
    aside           right after a major-version move puts the old cluster aside
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ontological.store import pg


def die_at(step: str) -> None:
    if step in ("pg_restore",):
        real_run = pg.run

        def run(cmd: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            if Path(cmd[0]).name == "pg_restore":
                os._exit(9)
            return real_run(cmd, **kw)

        setattr(pg, "run", run)  # noqa: B010
    elif step == "drop-store":
        real_psql = pg.psql

        def psql(layout: pg.Layout, q: str, **kw: Any) -> str:
            out = real_psql(layout, q, **kw)
            if q.startswith(f"DROP DATABASE {pg.DATABASE} "):
                os._exit(9)
            return out

        setattr(pg, "psql", psql)  # noqa: B010
    elif step in ("rename-initdb", "aside"):
        real_rename = Path.rename

        def rename(self: Path, target: Any) -> Path:
            moved = real_rename(self, target)
            if (step == "rename-initdb" and self.name == "pgdata.initdb") or (
                step == "aside" and Path(target).name.startswith("pgdata-")
            ):
                os._exit(9)
            return moved

        setattr(Path, "rename", rename)  # noqa: B010
    elif step != "prepare-store":
        raise SystemExit(f"unknown step {step}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("app_data", type=Path)
    ap.add_argument("bin", type=Path)
    ap.add_argument("--chains", action="store_true")
    ap.add_argument("--prepare-sql")
    ap.add_argument("--restore", type=Path)
    ap.add_argument("--migrate", action="store_true")
    ap.add_argument("--die-at")
    args = ap.parse_args(argv)
    layout = pg.Layout(args.app_data, args.bin)
    if args.die_at:
        die_at(args.die_at)

    def prepare(lay: pg.Layout) -> None:
        if args.die_at == "prepare-store":
            os._exit(9)
        if args.chains:
            pg.migrate_new_store(lay)
        if args.prepare_sql:
            pg.psql(lay, args.prepare_sql)

    t0 = time.monotonic()
    try:
        server = pg.ensure_running(layout, prepare_store=prepare)
        if args.restore:
            pg.restore(layout, args.restore)
            return 0
        if args.migrate:
            dump = pg.migrate(layout)
            print(json.dumps({"migrated": dump.name if dump else None}), flush=True)
            return 0
    except pg.PgError as e:
        print(json.dumps({"error": str(e), "type": type(e).__name__}), flush=True)
        return 1
    print(
        json.dumps(
            {
                "engine_pid": os.getpid(),
                "postmaster_pid": server.postmaster_pid,
                "action": server.action,
                "notes": list(server.notes),
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
    try:
        path = pg.stop(layout)
    except pg.PgError as e:
        print(json.dumps({"error": str(e), "type": type(e).__name__}), flush=True)
        return 1
    print(json.dumps({"quit_dump": path.name if path else None}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
