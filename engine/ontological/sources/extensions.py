"""DuckDB's extensions for sources, loaded from one directory with autoinstall off
(ARCHITECTURE.md §10).

    python -m ontological.sources.extensions <dir>

installs the scanners into <dir> for this DuckDB's version and platform, once, then checks that
every extension a source needs loads from there. The app bundles that directory, and the engine
names it with `--duckdb-extensions` (or `ONTOLOGICAL_DUCKDB_EXTENSIONS`). Every source session
connects with it as its `extension_directory`, with autoinstall and autoload off, and loads the
one scanner it needs by name, so a source opens with no network, and a missing extension is an
error that names the directory rather than a download.

`json` and `parquet` are linked into DuckDB's own library, so there is nothing to install for
them. Without a directory, DuckDB's default (`~/.duckdb/extensions`) is used, still with
autoinstall off. DuckDB checks each extension's signature when it loads it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb

ENV = "ONTOLOGICAL_DUCKDB_EXTENSIONS"

#: Installed into the directory: one scanner per attached kind of source.
INSTALLED = ("sqlite_scanner", "postgres_scanner", "mysql_scanner")
#: Linked into DuckDB itself: the file sources' readers.
BUILT_IN = ("json", "parquet")


class ExtensionError(Exception):
    """An extension isn't in the directory, or doesn't load from it."""


def directory() -> Path | None:
    """The directory the engine was given, which its executor children inherit."""
    value = os.environ.get(ENV)
    return Path(value) if value else None


def config(extension_dir: Path | None) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
    }
    if extension_dir is not None:
        settings["extension_directory"] = str(extension_dir)
    return settings


def connect(extension_dir: Path | None = None) -> duckdb.DuckDBPyConnection:
    """An in-memory connection that loads extensions only from `extension_dir` (by default the
    engine's, `directory()`), and only when asked."""
    return duckdb.connect(":memory:", config=config(extension_dir or directory()))


def load(con: duckdb.DuckDBPyConnection, name: str) -> None:
    try:
        con.load_extension(name)
    except duckdb.Error as e:
        where = con.execute("SELECT current_setting('extension_directory')").fetchone()
        raise ExtensionError(
            f"DuckDB's {name} extension doesn't load from {(where or [''])[0] or '~/.duckdb'}: "
            f"install it with `python -m ontological.sources.extensions <dir>` ({e})"
        ) from e


def install(extension_dir: Path) -> None:
    """Download the scanners for this DuckDB into `extension_dir`. Those already there stay."""
    extension_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:", config={"extension_directory": str(extension_dir)})
    try:
        for name in INSTALLED:
            con.install_extension(name)
    finally:
        con.close()


def verify(extension_dir: Path) -> None:
    """Raise ExtensionError unless every extension a source needs loads with autoinstall off:
    the scanners from `extension_dir`, the readers from DuckDB itself."""
    con = connect(extension_dir)
    try:
        for name in INSTALLED:
            load(con, name)
        loaded = {
            name
            for (name,) in con.execute(
                "SELECT extension_name FROM duckdb_extensions() WHERE loaded"
            ).fetchall()
        }
    finally:
        con.close()
    if missing := [n for n in (*INSTALLED, *BUILT_IN) if n not in loaded]:
        raise ExtensionError(f"not loaded: {', '.join(missing)}")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m ontological.sources.extensions <dir>", file=sys.stderr)
        return 2
    extension_dir = Path(args[0]).absolute()
    install(extension_dir)
    verify(extension_dir)
    print(f"DuckDB {duckdb.__version__}: {', '.join(INSTALLED)} in {extension_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
