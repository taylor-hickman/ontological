"""The sources the user adds and the catalog they browse (ARCHITECTURE.md §9.1; DESIGN.md §5.1,
§5.5): what the onboarding and catalog screens read through the API.

Adding a source opens it read-only through a source executor and ingests it (`ingest.py`): the
catalog, then the profile, then, with an embedder, the data layer. None of that spends Claude
quota. A server source's password goes to the keychain (`keychain.py`) before the executor opens
it, and the spec keeps only the reference.

A source is a catalog database row, named by `database_name`, which the store keeps unique. A
second source with the name of one already added, such as two files called `main.sqlite`, is
refused rather than diffed over the first.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import distinct, func, select

from auto_ontology.dal import schema as s
from auto_ontology.dal.reset import delete_all_data
from auto_ontology.dal.session import store
from ontological.catalog.ingest import ingest
from ontological.embed.embedder import LocalEmbedder
from ontological.jobs.active import ActiveJobs
from ontological.sources.executor import SourceExecutor
from ontological.sources.keychain import KeychainRef, forget_password, save_password
from ontological.sources.spec import (
    DuckDBSource,
    FileSource,
    MySQLSource,
    PostgresSource,
    ServerSource,
    Source,
    SqliteSource,
    from_record,
    to_record,
)

INGEST_TIMEOUT_S = 600.0  # as the staleness check's: only stops a wedged source

SourceKind = Literal["sqlite", "duckdb", "files", "postgres", "mysql", "unknown"]

_SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".db3"}
_DUCKDB_SUFFIXES = {".duckdb", ".ddb"}
_SQLITE_MAGIC = b"SQLite format 3\x00"
_DUCKDB_MAGIC = b"DUCK"  # at offset 8


class SourceError(Exception):
    """The request doesn't describe a source Ontological can open. The API answers 400."""


class SourceConflict(Exception):
    """A source with that database name is already added. The API answers 409."""


class NotFound(Exception):
    """No source or table with that id. The API answers 404."""


# --- requests -----------------------------------------------------------------------------------


class AddFiles(BaseModel):
    """A database file, or Parquet, CSV and JSON files read as the tables of one database."""

    kind: Literal["file"]
    paths: list[str] = Field(min_length=1)


class AddServer(BaseModel):
    kind: Literal["postgres", "mysql"]
    host: str = Field(min_length=1)
    port: int | None = None
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: str | None = None  # filed in the keychain, never kept


AddSource = AddFiles | AddServer


def _sniff(path: Path) -> Literal["sqlite", "duckdb"] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head.startswith(_SQLITE_MAGIC):
        return "sqlite"
    if head[8:12] == _DUCKDB_MAGIC:
        return "duckdb"
    return None


def files_source(paths: list[str]) -> Source:
    """The source for picked files: one SQLite or DuckDB database file, or data files."""
    resolved = [Path(p).expanduser() for p in paths]
    for path in resolved:
        if not path.is_absolute():
            raise SourceError(f"{path} isn't an absolute path")
        if not path.is_file():
            raise SourceError(f"{path} isn't a file")
    databases: list[Source] = []
    for path in resolved:
        suffix = path.suffix.lower()
        kind = (
            "sqlite"
            if suffix in _SQLITE_SUFFIXES
            else "duckdb"
            if suffix in _DUCKDB_SUFFIXES
            else _sniff(path)
        )
        if kind == "sqlite":
            databases.append(SqliteSource(path))
        elif kind == "duckdb":
            databases.append(DuckDBSource(path))
    if databases:
        if len(resolved) > 1:
            raise SourceError("add a database file on its own, not with other files")
        return databases[0]
    try:
        return FileSource.of(*resolved)
    except ValueError as e:
        raise SourceError(str(e)) from None


def server_source(request: AddServer, *, save: Callable[[str, str], KeychainRef]) -> ServerSource:
    """The server source, with its password filed in the keychain under the source's URL."""
    cls = PostgresSource if request.kind == "postgres" else MySQLSource
    port = request.port or (5432 if request.kind == "postgres" else 3306)
    account = f"{request.kind}://{request.user}@{request.host}:{port}/{request.database}"
    password = save(account, request.password) if request.password else None
    return cls(
        host=request.host,
        database=request.database,
        user=request.user,
        port=port,
        password=password,
    )


# --- responses ----------------------------------------------------------------------------------


class SourceOut(BaseModel):
    id: str
    name: str
    kind: SourceKind
    label: str  # the file's path, or the server's URL without a password
    size_bytes: int | None  # a file source's size on disk
    schemas: int
    tables: int
    columns: int
    pulled: datetime | None  # the last ingest


class Added(BaseModel):
    source: SourceOut
    seconds: float  # how long reading the source took
    skipped: int  # relations the executor couldn't read


class TableOut(BaseModel):
    id: str
    name: str
    table_type: str | None
    columns: int
    pk: list[str]
    description: str | None


class SchemaOut(BaseModel):
    id: str
    name: str
    tables: list[TableOut]


class SourceCatalog(BaseModel):
    source: SourceOut
    schemas: list[SchemaOut]


class ColumnOut(BaseModel):
    id: str
    name: str
    data_type: str | None
    is_nullable: bool | None
    is_unique: bool | None
    format: str | None  # an inferred date format, such as YYYY-MM-DD
    sample_values: list[str]
    description: str | None
    pk: bool
    references: list[str]  # `schema.table.column` for each foreign key from this column
    tags: list[str]
    pii: bool


class TableDetail(BaseModel):
    id: str
    source_id: str
    source: str
    schema_name: str
    name: str
    table_type: str | None
    description: str | None
    columns: list[ColumnOut]
    tags: list[str]


class Sources(Protocol):
    """What the API's source and catalog routes call. Every method may block."""

    def list(self) -> list[SourceOut]: ...
    def add(self, request: AddSource) -> Added: ...
    def remove(self, source_id: str) -> None: ...
    def catalog(self, source_id: str) -> SourceCatalog: ...
    def table(self, table_id: str) -> TableDetail: ...


# --- the store ----------------------------------------------------------------------------------


def _describe(name: str, connection: Any) -> tuple[SourceKind, str, int | None]:
    """A source's kind, label and size on disk from the spec its row keeps."""
    if connection is None:
        return "unknown", name, None
    try:
        source = from_record(connection)
    except ValueError:
        return "unknown", name, None
    record = to_record(source)
    kind: SourceKind = record["kind"]
    size: int | None = None
    if isinstance(source, SqliteSource | DuckDBSource):
        size = _size(source.path)
    elif isinstance(source, FileSource):
        sizes = [_size(Path(t.path)) for t in source.tables]
        size = None if None in sizes else sum(x for x in sizes if x is not None)
    return kind, source.label, size


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _samples(raw: str | None) -> list[str]:
    """`sample_values` is a JSON list kept as text; render each value as a string."""
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(values, list):
        return []
    return [v if isinstance(v, str) else json.dumps(v) for v in values]


class StoreSources:
    """Sources in the bundled store. `open_executor` and `save_password` are seams for tests."""

    def __init__(
        self,
        *,
        jobs: ActiveJobs,
        embedder: LocalEmbedder | None = None,
        open_executor: Callable[[Source], SourceExecutor] = SourceExecutor,
        save: Callable[[str, str], KeychainRef] = save_password,
        forget: Callable[[KeychainRef], None] = forget_password,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._jobs = jobs
        self._embedder = embedder
        self._open = open_executor
        self._save = save
        self._forget = forget
        self._clock = clock

    def list(self) -> list[SourceOut]:
        return self._summaries()

    def _summaries(self, source_id: str | None = None) -> list[SourceOut]:
        db, sc, t, c = s.catalog_database, s.catalog_schema, s.catalog_table, s.catalog_column
        query = (
            select(
                db.c.id,
                db.c.name,
                db.c.connection,
                db.c.pulled,
                func.count(distinct(sc.c.id)).label("schemas"),
                func.count(distinct(t.c.id)).label("tables"),
                func.count(distinct(c.c.id)).label("columns"),
            )
            .select_from(
                db.outerjoin(sc, sc.c.database_id == db.c.id)
                .outerjoin(t, t.c.schema_id == sc.c.id)
                .outerjoin(c, c.c.table_id == t.c.id)
            )
            .group_by(db.c.id)
            .order_by(db.c.name)
        )
        if source_id is not None:
            query = query.where(db.c.id == source_id)
        out: list[SourceOut] = []
        for row in store().query_read(query):
            kind, label, size = _describe(row["name"], row["connection"])
            out.append(
                SourceOut(
                    id=row["id"],
                    name=row["name"],
                    kind=kind,
                    label=label,
                    size_bytes=size,
                    schemas=row["schemas"],
                    tables=row["tables"],
                    columns=row["columns"],
                    pulled=row["pulled"],
                )
            )
        return out

    def _summary(self, source_id: str) -> SourceOut:
        found = self._summaries(source_id)
        if not found:
            raise NotFound(f"no source {source_id}")
        return found[0]

    def add(self, request: AddSource) -> Added:
        if isinstance(request, AddFiles):
            source = files_source(request.paths)
            self._refuse_taken(source.database_name)
        else:
            # Before the password is filed: the same server's account may hold the password of
            # the source already added under that name.
            self._refuse_taken(request.database)
            source = server_source(request, save=self._save)
        try:
            return self._ingest(source)
        except BaseException:
            # The password was filed for this source; nothing refers to it once the add fails.
            if isinstance(source, PostgresSource | MySQLSource) and source.password is not None:
                self._forget(source.password)
            raise

    def _refuse_taken(self, name: str) -> None:
        db = s.catalog_database
        if store().query_read(select(db.c.id).where(db.c.name == name)):
            raise SourceConflict(f"a source named {name} is already added")

    def _ingest(self, source: Source) -> Added:
        name = source.database_name
        started = self._clock()
        with self._jobs.track("ingest", name), self._open(source) as executor:
            got = ingest(executor, embedder=self._embedder, timeout_s=INGEST_TIMEOUT_S)
        if got.database_id is None:
            raise SourceError(f"{source.label} has no tables Ontological can read")
        return Added(
            source=self._summary(got.database_id),
            seconds=round(self._clock() - started, 2),
            skipped=len(got.skipped),
        )

    def remove(self, source_id: str) -> None:
        db = s.catalog_database
        rows = store().query_read(select(db.c.name, db.c.connection).where(db.c.id == source_id))
        if not rows:
            raise NotFound(f"no source {source_id}")
        name, connection = rows[0]["name"], rows[0]["connection"]
        delete_all_data(name)  # never None, which would delete every database
        account = connection.get("password") if isinstance(connection, dict) else None
        if account:
            self._forget(KeychainRef(str(account)))

    def catalog(self, source_id: str) -> SourceCatalog:
        summary = self._summary(source_id)
        sc, t, c = s.catalog_schema, s.catalog_table, s.catalog_column
        rows = store().query_read(
            select(
                sc.c.id.label("schema_id"),
                sc.c.name.label("schema_name"),
                t.c.id,
                t.c.name,
                t.c.table_type,
                t.c.pk,
                t.c.description,
                func.count(c.c.id).label("columns"),
            )
            .select_from(
                sc.outerjoin(t, t.c.schema_id == sc.c.id).outerjoin(c, c.c.table_id == t.c.id)
            )
            .where(sc.c.database_id == source_id)
            .group_by(sc.c.id, t.c.id)
            .order_by(func.lower(sc.c.name), func.lower(t.c.name))
        )
        schemas: dict[str, SchemaOut] = {}
        for row in rows:
            schema = schemas.setdefault(
                row["schema_id"], SchemaOut(id=row["schema_id"], name=row["schema_name"], tables=[])
            )
            if row["id"] is not None:
                schema.tables.append(
                    TableOut(
                        id=row["id"],
                        name=row["name"],
                        table_type=row["table_type"],
                        columns=row["columns"],
                        pk=list(row["pk"] or []),
                        description=row["description"],
                    )
                )
        return SourceCatalog(source=summary, schemas=list(schemas.values()))

    def table(self, table_id: str) -> TableDetail:
        db, sc, t, c = s.catalog_database, s.catalog_schema, s.catalog_table, s.catalog_column
        head = store().query_read(
            select(
                db.c.id.label("source_id"),
                db.c.name.label("source"),
                sc.c.name.label("schema_name"),
                t.c.name,
                t.c.table_type,
                t.c.description,
                t.c.pk,
            )
            .select_from(t.join(sc, sc.c.id == t.c.schema_id).join(db, db.c.id == sc.c.database_id))
            .where(t.c.id == table_id)
        )
        if not head:
            raise NotFound(f"no table {table_id}")
        info = head[0]
        pk = set(info["pk"] or [])
        columns = store().query_read(
            select(
                c.c.id,
                c.c.name,
                c.c.data_type,
                c.c.is_nullable,
                c.c.is_unique,
                c.c.format,
                c.c.sample_values,
                c.c.description,
            )
            .where(c.c.table_id == table_id)
            .order_by(c.c.ordinal_position, c.c.name)
        )
        tags = self._tags(table_id, [row["id"] for row in columns])
        refs = self._references([row["id"] for row in columns])
        return TableDetail(
            id=table_id,
            source_id=info["source_id"],
            source=info["source"],
            schema_name=info["schema_name"],
            name=info["name"],
            table_type=info["table_type"],
            description=info["description"],
            tags=tags.get(table_id, []),
            columns=[
                ColumnOut(
                    id=row["id"],
                    name=row["name"],
                    data_type=row["data_type"],
                    is_nullable=row["is_nullable"],
                    is_unique=row["is_unique"],
                    format=row["format"],
                    sample_values=_samples(row["sample_values"]),
                    description=row["description"],
                    pk=row["name"] in pk,
                    references=refs.get(row["id"], []),
                    tags=tags.get(row["id"], []),
                    pii=any(tag.strip().lower() == "pii" for tag in tags.get(row["id"], [])),
                )
                for row in columns
            ],
        )

    def _tags(self, table_id: str, column_ids: list[str]) -> dict[str, list[str]]:
        """Tag names by the table's id and each column's id."""
        tag, target = s.tag, s.tag_target
        rows = store().query_read(
            select(target.c.table_id, target.c.column_id, tag.c.name)
            .select_from(target.join(tag, tag.c.id == target.c.tag_id))
            .where((target.c.table_id == table_id) | target.c.column_id.in_(column_ids))
            .order_by(func.lower(tag.c.name))
        )
        out: dict[str, list[str]] = {}
        for row in rows:
            key = row["column_id"] or row["table_id"]
            out.setdefault(key, []).append(row["name"])
        return out

    def _references(self, column_ids: list[str]) -> dict[str, list[str]]:
        """`schema.table.column` for each foreign key's target, by its source column."""
        fk, c, t, sc = s.column__foreign_key, s.catalog_column, s.catalog_table, s.catalog_schema
        rows = store().query_read(
            select(
                fk.c.source_column_id, sc.c.name.label("schema"), t.c.name.label("table"), c.c.name
            )
            .select_from(
                fk.join(c, c.c.id == fk.c.target_column_id)
                .join(t, t.c.id == c.c.table_id)
                .join(sc, sc.c.id == t.c.schema_id)
            )
            .where(fk.c.source_column_id.in_(column_ids))
            .order_by(sc.c.name, t.c.name, c.c.name)
        )
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["source_column_id"], []).append(
                f"{row['schema']}.{row['table']}.{row['name']}"
            )
        return out
