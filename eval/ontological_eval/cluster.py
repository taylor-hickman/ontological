"""The eval's own Postgres cluster, which holds Wide World Importers (BUILD_PLAN.md, Eval set).

It runs on the bundled binaries through the store's lifecycle code (`ontological.store.pg`,
ARCHITECTURE.md §5.3), under <home>/cluster/ instead of the app's <appData>, so it never touches
the store. WWI's gold SQL runs here natively, and M2 ingests it from here as a Postgres source.

WWI is loaded during the cluster's first launch, inside pgdata.initdb/, the way the store's
Alembic chains run: the DDL before the data (schemas, sequences, tables, indexes), each CSV with
a server-side COPY, then the foreign keys and views, all in one transaction, and then each table's
row count is checked against its CSV. A load that fails or comes up short never becomes pgdata/,
so the next `up` loads again from the beginning.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ontological.store import pg

from .home import cluster_dir, wwi_dir

DATABASE = "wideworldimporters"
DDL_BEFORE_DATA = ("01_schemas.sql", "02_sequences.sql", "03_tables.sql", "04_indexes.sql")
DDL_AFTER_DATA = ("05_fkeys.sql", "06_views.sql")
# Some WWI fields (photos, JSON) are longer than the csv module's default 128 KB limit.
MAX_FIELD = 1 << 30


def layout(home: Path, pg_bin: Path) -> pg.Layout:
    return pg.Layout(cluster_dir(home), pg_bin)


def check_socket_path(lay: pg.Layout) -> None:
    n = len(str(lay.socket).encode())
    if n > pg.MAX_SOCKET_PATH:
        raise pg.PgError(
            f"The eval cluster's socket path {lay.socket} is {n} bytes, and macOS allows "
            f"{pg.MAX_SOCKET_PATH}. Set ONTOLOGICAL_EVAL_HOME to a shorter directory."
        )


def tables(wwi: Path) -> dict[str, Path]:
    """`schema.table` for each CSV in wwi/data/, named <Schema>.<Table>.csv. The DDL doesn't
    quote its identifiers, so Postgres folds them to lower case."""
    csvs = sorted((wwi / "data").glob("*.csv"))
    if not csvs:
        raise pg.PgError(f"no WWI CSVs in {wwi / 'data'}: run `fetch wwi` first")
    return {p.stem.lower(): p for p in csvs}


def csv_rows(path: Path) -> int:
    """Records in a CSV, not counting the header. Quoted fields can hold newlines, so this
    parses the file rather than counting lines."""
    csv.field_size_limit(MAX_FIELD)
    with open(path, newline="", encoding="utf-8") as f:
        return sum(1 for _ in csv.reader(f)) - 1


def _literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def load_sql(wwi: Path) -> str:
    ddl = wwi / "ddl"
    copies = [
        f"COPY {table} FROM {_literal(str(path))} WITH (FORMAT csv, HEADER true, NULL '\\N');"
        for table, path in tables(wwi).items()
    ]
    return "\n".join(
        [*((ddl / f).read_text() for f in DDL_BEFORE_DATA), *copies]
        + [(ddl / f).read_text() for f in DDL_AFTER_DATA]
    )


def row_counts(lay: pg.Layout, names: list[str]) -> dict[str, int]:
    query = " UNION ALL ".join(f"SELECT {_literal(t)}, count(*) FROM {t}" for t in names)
    out = pg.psql(lay, query, db=DATABASE)
    return {t: int(n) for t, n in (line.split("|") for line in out.splitlines())}


def mismatches(lay: pg.Layout, wwi: Path) -> dict[str, tuple[int, int]]:
    """Tables whose row count differs from their CSV's: {table: (in the CSV, in Postgres)}."""
    src = {t: csv_rows(p) for t, p in tables(wwi).items()}
    got = row_counts(lay, list(src))
    return {t: (n, got[t]) for t, n in src.items() if got[t] != n}


def load_wwi(lay: pg.Layout, wwi: Path) -> None:
    """Create the WWI database and load it; raise if any table's count is off."""
    pg.psql(lay, f"CREATE DATABASE {DATABASE}", db="postgres")
    # psql -c sends the whole string as one query, so it all commits or none of it does.
    pg.psql(lay, load_sql(wwi), db=DATABASE)
    if bad := mismatches(lay, wwi):
        raise pg.PgError(f"WWI row counts differ from the CSVs (CSV, Postgres): {bad}")


def up(home: Path, pg_bin: Path) -> pg.Server:
    """Start or adopt the eval cluster, loading WWI on its first launch."""
    lay = layout(home, pg_bin)
    check_socket_path(lay)
    wwi = wwi_dir(home)
    tables(wwi)  # fail before initdb if the data isn't there
    return pg.ensure_running(lay, prepare_store=lambda staging: load_wwi(staging, wwi))


def down(home: Path, pg_bin: Path) -> None:
    """Stop the eval cluster if it's running. There's no quit dump: the data comes from the
    download and can be loaded again."""
    pg.stop(layout(home, pg_bin), quit_dump=False)


def running(home: Path, pg_bin: Path) -> pg.PidFile | None:
    state = pg.inspect_pid_file(layout(home, pg_bin))
    return state.pid_file if isinstance(state, pg.Running) else None
