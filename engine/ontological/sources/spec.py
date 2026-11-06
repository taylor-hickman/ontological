"""What a source is: the plain, picklable description an executor opens (ARCHITECTURE.md §5.5).

A spec holds no secret. A server source names its password by a `KeychainRef`, and the executor
reads the keychain each time it starts a child, so the store and the spec only ever hold the
reference (§4.3, Source credentials). Ingest keeps each source's spec, as `to_record` writes it, on
its catalog database row, so the engine can open the source again (§9.1, the staleness check).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ontological.sources.keychain import KeychainRef

FileFormat = Literal["parquet", "csv", "json"]

_SUFFIXES: dict[str, FileFormat] = {
    ".parquet": "parquet",
    ".csv": "csv",
    ".tsv": "csv",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
}
_COMPRESSION = (".gz", ".zst")


def file_format(path: str | Path) -> FileFormat:
    """The format a file's suffix names, looking past `.gz` or `.zst`."""
    suffixes = [s.lower() for s in Path(path).suffixes]
    if suffixes and suffixes[-1] in _COMPRESSION:
        suffixes.pop()
    if suffixes and suffixes[-1] in _SUFFIXES:
        return _SUFFIXES[suffixes[-1]]
    raise ValueError(f"can't tell the format of {path}: name it as parquet, csv or json")


def _stem(path: str | Path) -> str:
    name = Path(path).name
    while Path(name).suffix.lower() in (*_SUFFIXES, *_COMPRESSION):
        name = Path(name).stem
    return name


@dataclass(frozen=True)
class SqliteSource:
    """A SQLite file, attached through DuckDB's SQLite scanner."""

    path: Path

    @property
    def database_name(self) -> str:
        return self.path.stem

    @property
    def label(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class DuckDBSource:
    """A DuckDB database file, attached read-only."""

    path: Path

    @property
    def database_name(self) -> str:
        return self.path.stem

    @property
    def label(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class FileTable:
    """One table read from a Parquet, CSV or JSON file. `path` may be a glob, such as
    `sales/*.parquet`, for a table split across files."""

    name: str
    path: str
    format: FileFormat

    @classmethod
    def of(cls, path: str | Path, *, name: str | None = None) -> FileTable:
        """A table named after its file, in the format its suffix names."""
        return cls(name or _stem(path), str(path), file_format(path))


@dataclass(frozen=True)
class FileSource:
    """Parquet, CSV and JSON files read as the tables of one database."""

    name: str
    tables: tuple[FileTable, ...]

    def __post_init__(self) -> None:
        if not self.tables:
            raise ValueError("a file source needs at least one file")
        names = [t.name.lower() for t in self.tables]  # DuckDB's names are case-insensitive
        if len(set(names)) != len(names):
            raise ValueError(f"two files would make the same table: {sorted(names)}")

    @classmethod
    def of(cls, *paths: str | Path, name: str | None = None) -> FileSource:
        """One table per file, named after it; the database is named after the first file's
        folder unless `name` is given."""
        if not paths:
            raise ValueError("a file source needs at least one file")
        return cls(name or Path(paths[0]).parent.name, tuple(FileTable.of(p) for p in paths))

    @property
    def database_name(self) -> str:
        return self.name

    @property
    def label(self) -> str:
        return self.name


@dataclass(frozen=True)
class PostgresSource:
    """A Postgres database read through DuckDB's `postgres_scanner`. `host` may be a Unix
    socket directory."""

    host: str
    database: str
    user: str
    port: int = 5432
    password: KeychainRef | None = field(default=None)

    @property
    def database_name(self) -> str:
        return self.database

    @property
    def label(self) -> str:
        return f"postgres://{self.user}@{self.host}:{self.port}/{self.database}"


@dataclass(frozen=True)
class MySQLSource:
    """A MySQL database read through DuckDB's `mysql_scanner`."""

    host: str
    database: str
    user: str
    port: int = 3306
    password: KeychainRef | None = field(default=None)

    @property
    def database_name(self) -> str:
        return self.database

    @property
    def label(self) -> str:
        return f"mysql://{self.user}@{self.host}:{self.port}/{self.database}"


Source = SqliteSource | DuckDBSource | FileSource | PostgresSource | MySQLSource
ServerSource = PostgresSource | MySQLSource


# --- a source as JSON, the form the store keeps ------------------------------------------------

Record = dict[str, Any]


def to_record(source: Source) -> Record:
    """The source as JSON, for `catalog_database.connection`. A password stays a reference."""
    if isinstance(source, SqliteSource | DuckDBSource):
        kind = "sqlite" if isinstance(source, SqliteSource) else "duckdb"
        return {"kind": kind, "path": str(source.path)}
    if isinstance(source, FileSource):
        return {
            "kind": "files",
            "name": source.name,
            "tables": [{"name": t.name, "path": t.path, "format": t.format} for t in source.tables],
        }
    return {
        "kind": "postgres" if isinstance(source, PostgresSource) else "mysql",
        "host": source.host,
        "database": source.database,
        "user": source.user,
        "port": source.port,
        "password": source.password.account if source.password else None,
    }


def from_record(record: Mapping[str, Any]) -> Source:
    """The source `to_record` wrote. Raises `ValueError` for a record it can't read."""
    try:
        kind = record["kind"]
        if kind in ("sqlite", "duckdb"):
            return (SqliteSource if kind == "sqlite" else DuckDBSource)(Path(record["path"]))
        if kind == "files":
            return FileSource(
                record["name"],
                tuple(FileTable(t["name"], t["path"], t["format"]) for t in record["tables"]),
            )
        if kind in ("postgres", "mysql"):
            password = record.get("password")
            return (PostgresSource if kind == "postgres" else MySQLSource)(
                host=record["host"],
                database=record["database"],
                user=record["user"],
                port=int(record["port"]),
                password=KeychainRef(password) if password else None,
            )
    except (KeyError, TypeError) as e:
        raise ValueError(f"not a source record: {record!r}") from e
    raise ValueError(f"not a source record: {record!r}")
