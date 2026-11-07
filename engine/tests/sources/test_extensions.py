"""DuckDB's extensions load only from the engine's directory, with autoinstall off (ARCHITECTURE.md
§10). Each test runs a real executor process, which inherits the directory the way the engine's
children do.

The directory the tests use is ONTOLOGICAL_DUCKDB_EXTENSIONS (CI installs it with
`python -m ontological.sources.extensions`), or DuckDB's default without it."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ontological.sources import extensions
from ontological.sources.executor import SourceError, SourceExecutor, SqliteSource


def make_sqlite(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    return path


def test_a_session_runs_with_autoinstall_and_autoload_off(tmp_path: Path) -> None:
    ex = SourceExecutor(SqliteSource(make_sqlite(tmp_path / "a.db")))
    try:
        rows = ex.run_sql(
            "SELECT current_setting('autoinstall_known_extensions'), "
            "current_setting('autoload_known_extensions')"
        ).rows
    finally:
        ex.close()
    assert rows == [(False, False)]


def test_a_session_uses_the_engine_s_extension_directory(tmp_path: Path) -> None:
    ex = SourceExecutor(SqliteSource(make_sqlite(tmp_path / "a.db")))
    try:
        rows = ex.run_sql("SELECT current_setting('extension_directory')").rows
    finally:
        ex.close()
    assert rows == [(str(extensions.directory() or ""),)]


def test_an_extension_missing_from_the_directory_is_an_error_not_a_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "extensions"
    empty.mkdir()
    monkeypatch.setenv(extensions.ENV, str(empty))
    ex = SourceExecutor(SqliteSource(make_sqlite(tmp_path / "a.db")))
    try:
        missing = f"sqlite_scanner extension doesn't load from {empty}"
        with pytest.raises(SourceError, match=missing):
            ex.run_sql("SELECT 1")
    finally:
        ex.close()
    assert not any(empty.rglob("*.duckdb_extension"))


def test_verify_names_the_extensions_that_don_t_load(tmp_path: Path) -> None:
    with pytest.raises(extensions.ExtensionError, match="sqlite_scanner"):
        extensions.verify(tmp_path)


def test_the_engine_s_directory_has_every_source_extension() -> None:
    directory = extensions.directory()
    if directory is None:
        pytest.skip(f"{extensions.ENV} is unset")
    extensions.verify(directory)
