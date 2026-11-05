"""uv run python -m ontological_eval <command>

    fetch [wwi | bird-questions | bird-databases ...]   download the eval data (default: all)
    cluster up        start or adopt the eval cluster, loading WWI on its first launch
    cluster check     compare each WWI table's row count with its CSV
    cluster status    whether it's running, and how to connect
    cluster down      stop it

The data and the cluster live under ONTOLOGICAL_EVAL_HOME (default ~/Library/Application
Support/Ontological Eval). The cluster runs the bundled Postgres binaries in ONTOLOGICAL_PG_BIN
or --pg-bin.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ontological.store import pg

from . import cluster, fetch
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
    args = parser.parse_args(argv)
    home = eval_home()

    try:
        if args.command == "fetch":
            sources = fetch.load_sources()
            written = fetch.fetch(home, args.groups or list(sources), sources=sources)
            print(f"{len(written)} file(s) downloaded; the data is current in {home}")
            return 0

        pg_bin = _pg_bin(args.pg_bin)
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
    except (fetch.FetchError, pg.PgError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _connect(lay: pg.Layout) -> str:
    return (
        f'connect: {lay.tool("psql")} -h "{lay.pgsocket}" -p {pg.PORT} -U {pg.USER} '
        f"-d {cluster.DATABASE}"
    )


if __name__ == "__main__":
    sys.exit(main())
