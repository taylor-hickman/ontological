"""Sources and Claude for the stub engine (serve_stub.py), which has no store.

`FakeSources` keeps sources in memory and reads a SQLite file's tables, columns and keys with
Python's sqlite3, so the desktop app's tests can add a real file and browse it. `FakeClaude`
detects the `claude` the engine is pointed at, as the real engine does, and answers the test call
without spawning anything.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ontological.catalog.sources import (
    Added,
    AddFiles,
    AddSource,
    ColumnOut,
    NotFound,
    SchemaOut,
    SourceCatalog,
    SourceConflict,
    SourceError,
    SourceOut,
    TableDetail,
    TableOut,
    files_source,
)
from ontological.llm.install import ClaudeInstall, PingResult, detect
from ontological.sources.spec import SqliteSource


class FakeClaude:
    def __init__(self, cli_path: Path | None) -> None:
        self._cli_path = cli_path

    async def detect(self) -> ClaudeInstall:
        return await detect(self._cli_path)

    async def test_call(self) -> PingResult:
        return PingResult(ok=True, seconds=0.4, state="ready", error=None)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class FakeSources:
    def __init__(self) -> None:
        self._sources: dict[str, SourceOut] = {}
        self._catalogs: dict[str, SourceCatalog] = {}
        self._tables: dict[str, TableDetail] = {}

    @classmethod
    def seeded(cls, path: Path) -> FakeSources:
        """Sources listed in `path`, a JSON list of SourceOut, when it exists; their catalogs
        are empty."""
        sources = cls()
        if path.exists():
            for row in json.loads(path.read_text()):
                source = SourceOut.model_validate(row)
                sources._sources[source.id] = source
                sources._catalogs[source.id] = SourceCatalog(source=source, schemas=[])
        return sources

    def list(self) -> list[SourceOut]:
        return sorted(self._sources.values(), key=lambda s: s.name)

    def add(self, request: AddSource) -> Added:
        if not isinstance(request, AddFiles):
            raise SourceError("the stub engine reads only SQLite files")
        source = files_source(request.paths)
        if not isinstance(source, SqliteSource):
            raise SourceError("the stub engine reads only SQLite files")
        if any(s.name == source.database_name for s in self._sources.values()):
            raise SourceConflict(f"a source named {source.database_name} is already added")
        source_id = uuid.uuid4().hex
        schema = SchemaOut(id=uuid.uuid4().hex, name="main", tables=[])
        columns = 0
        with sqlite3.connect(f"file:{source.path}?mode=ro", uri=True) as con:
            names = [
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY lower(name)"
                )
            ]
            for name in names:
                table_id = uuid.uuid4().hex
                info = con.execute(f"PRAGMA table_info({_ident(name)})").fetchall()
                fks = con.execute(f"PRAGMA foreign_key_list({_ident(name)})").fetchall()
                refs: dict[str, list[str]] = {}
                for fk in fks:  # id, seq, table, from, to, ...
                    refs.setdefault(fk[3], []).append(f"main.{fk[2]}.{fk[4]}")
                pk = [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]]
                cols: list[ColumnOut] = []
                for _cid, col, data_type, notnull, _default, in_pk in info:
                    samples = [
                        str(v[0])
                        for v in con.execute(
                            f"SELECT DISTINCT {_ident(col)} FROM {_ident(name)} "
                            f"WHERE {_ident(col)} IS NOT NULL LIMIT 3"
                        )
                    ]
                    cols.append(
                        ColumnOut(
                            id=uuid.uuid4().hex,
                            name=col,
                            data_type=data_type or None,
                            is_nullable=not notnull and not in_pk,
                            is_unique=bool(in_pk) or None,
                            format=None,
                            sample_values=samples,
                            description=None,
                            pk=bool(in_pk),
                            references=refs.get(col, []),
                            tags=[],
                            pii=False,
                        )
                    )
                columns += len(cols)
                schema.tables.append(
                    TableOut(
                        id=table_id,
                        name=name,
                        table_type="base table",
                        columns=len(cols),
                        pk=pk,
                        description=None,
                    )
                )
                self._tables[table_id] = TableDetail(
                    id=table_id,
                    source_id=source_id,
                    source=source.database_name,
                    schema_name="main",
                    name=name,
                    table_type="base table",
                    description=None,
                    columns=cols,
                    tags=[],
                )
        if not schema.tables:
            raise SourceError(f"{source.label} has no tables Ontological can read")
        summary = SourceOut(
            id=source_id,
            name=source.database_name,
            kind="sqlite",
            label=source.label,
            size_bytes=os.path.getsize(source.path),
            schemas=1,
            tables=len(schema.tables),
            columns=columns,
            pulled=datetime.now(UTC).replace(microsecond=0),
        )
        self._sources[source_id] = summary
        self._catalogs[source_id] = SourceCatalog(source=summary, schemas=[schema])
        return Added(source=summary, seconds=0.1, skipped=0)

    def remove(self, source_id: str) -> None:
        if self._sources.pop(source_id, None) is None:
            raise NotFound(f"no source {source_id}")
        del self._catalogs[source_id]
        self._tables = {k: t for k, t in self._tables.items() if t.source_id != source_id}

    def catalog(self, source_id: str) -> SourceCatalog:
        try:
            return self._catalogs[source_id]
        except KeyError:
            raise NotFound(f"no source {source_id}") from None

    def table(self, table_id: str) -> TableDetail:
        try:
            return self._tables[table_id]
        except KeyError:
            raise NotFound(f"no table {table_id}") from None
