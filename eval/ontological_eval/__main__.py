"""uv run python -m ontological_eval <command>

    fetch [wwi | bird-questions | bird-databases ...]   download the eval data (default: all)
    cluster up        start or adopt the eval cluster, loading WWI on its first launch
    cluster check     compare each WWI table's row count with its CSV
    cluster status    whether it's running, and how to connect
    cluster down      stop it
    ingest [wwi | <bird db> ...]   ingest the eval databases into the eval store (default: all)
    sets freeze [--force]   choose eval/sets/{dev,heldout,smoke}.json with the fixed seed
    sets check        run every set's gold SQL: each must run and return rows (needs `cluster up`)

The data, the cluster and the eval store live under ONTOLOGICAL_EVAL_HOME (default
~/Library/Application Support/Ontological Eval). The cluster and the store run the bundled
Postgres binaries in ONTOLOGICAL_PG_BIN or --pg-bin.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

from ontological.store import pg

from . import cluster, fetch, ingest, questions, sets
from .home import eval_home, wwi_dir


def _pg_bin(arg: str | None) -> Path:
    value = arg or os.environ.get("ONTOLOGICAL_PG_BIN")
    if not value:
        sys.exit("set ONTOLOGICAL_PG_BIN or pass --pg-bin: the bundled Postgres's bin/ directory")
    return Path(value).expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ontological_eval", description="Ontological's eval data and its WWI cluster"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="download the eval data")
    f.add_argument("groups", nargs="*", help="source groups (default: all)")
    c = sub.add_parser("cluster", help="the eval Postgres cluster that holds WWI")
    c.add_argument("action", choices=["up", "check", "status", "down"])
    c.add_argument("--pg-bin", help="the bundled Postgres's bin/ (default: $ONTOLOGICAL_PG_BIN)")
    i = sub.add_parser("ingest", help="ingest WWI and the BIRD databases into the eval store")
    i.add_argument(
        "databases", nargs="*", help=f"any of {', '.join(ingest.DATABASES)} (default: all)"
    )
    i.add_argument("--pg-bin", help="the bundled Postgres's bin/ (default: $ONTOLOGICAL_PG_BIN)")
    s = sub.add_parser("sets", help="the frozen question sets in eval/sets/")
    s.add_argument("action", choices=["freeze", "check"])
    s.add_argument("--force", action="store_true", help="freeze: rewrite existing set files")
    args = parser.parse_args(argv)
    home = eval_home()

    try:
        if args.command == "fetch":
            sources = fetch.load_sources()
            written = fetch.fetch(home, args.groups or list(sources), sources=sources)
            print(f"{len(written)} file(s) downloaded; the data is current in {home}")
            return 0
        if args.command == "sets":
            return _sets(args.action, home, force=args.force)

        pg_bin = _pg_bin(args.pg_bin)
        if args.command == "ingest":
            return _ingest(home, pg_bin, args.databases or list(ingest.DATABASES))
        lay = cluster.layout(home, pg_bin)
        if args.action == "up":
            server = cluster.up(home, pg_bin)
            print(f"eval cluster {server.action}, postmaster pid {server.postmaster_pid}")
            print(_connect(lay))
        elif args.action == "check":
            if bad := cluster.mismatches(lay, wwi_dir(home)):
                for t, (want, got) in bad.items():
                    print(f"{t}: {want} rows in the CSV, {got} in Postgres")
                return 1
            print(f"all {len(cluster.tables(wwi_dir(home)))} WWI tables match their CSVs")
        elif args.action == "status":
            pf = cluster.running(home, pg_bin)
            print(f"running, pid {pf.pid}\n{_connect(lay)}" if pf else "not running")
        else:
            cluster.down(home, pg_bin)
            print("eval cluster stopped")
        return 0
    except (fetch.FetchError, pg.PgError, sets.SetsError, ingest.IngestError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _ingest(home: Path, pg_bin: Path, names: list[str]) -> int:
    for r in ingest.run(home, pg_bin, names):
        if r.ingested is None:
            print(f"{r.name}: already in the store, left as it is")
            continue
        got = r.ingested
        print(f"{r.name}: {got.tables} tables, {got.columns} columns as {got.database_name}")
        for s in got.skipped:
            print(f"  skipped {s['table_schema']}.{s['table_name']}: {s['error']}")
    print(f"the eval store is {ingest.layout(home, pg_bin).app_data}")
    return 0


def _sets(action: str, home: Path, *, force: bool) -> int:
    if action == "freeze":
        chosen = sets.freeze(home, force=force)
        for name in sets.NAMES:
            n = Counter(source for source, _ in chosen[name])
            print(f"{name}: {n['wwi']} WWI, {n['bird']} BIRD -> {sets.SETS_DIR / name}.json")
        return 0
    frozen = sets.read()
    runner = sets.GoldRunner(home)
    try:
        problems = sets.check(frozen, questions.load(home), runner)
    finally:
        runner.close()
    for p in problems:
        print(p)
    if problems:
        return 1
    n = len({k for name in sets.NAMES for k in frozen[name]})
    print(f"all {n} gold queries in dev, heldout and smoke run and return rows")
    return 0


def _connect(lay: pg.Layout) -> str:
    return (
        f'connect: {lay.tool("psql")} -h "{lay.pgsocket}" -p {pg.PORT} -U {pg.USER} '
        f"-d {cluster.DATABASE}"
    )


if __name__ == "__main__":
    sys.exit(main())
