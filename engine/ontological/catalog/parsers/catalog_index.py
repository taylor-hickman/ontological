"""A database's catalog as the parse pool's workers read it: names and ids only, picklable, with
no store behind it (ARCHITECTURE.md §5.6).

Upstream resolves a query's tables and columns through its `Schema` objects, which hold DataFrames
and look each name up by filtering them. The workers need only the names and the ids they map to,
so the engine reads them from the store once per ingest and sends this in their place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property


@dataclass(frozen=True)
class TableEntry:
    id: str
    name: str  # as the catalog spells it
    columns: dict[str, str] = field(default_factory=dict[str, str])  # lower-cased name -> id
    # lower-cased name -> the name as the catalog spells it, for messages (the answer gate's)
    spellings: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class SchemaEntry:
    tables: dict[str, TableEntry] = field(default_factory=dict[str, TableEntry])  # by lower name


@dataclass(frozen=True)
class CatalogIndex:
    """`{lower-cased schema name: SchemaEntry}`. A table named without a schema resolves to the
    first schema holding it, in this order, as upstream's dict of `Schema`s did."""

    schemas: dict[str, SchemaEntry] = field(default_factory=dict[str, SchemaEntry])

    @cached_property
    def flat(self) -> dict[str, dict[str, str]]:
        """`{table: {column: "TEXT"}}` over every schema, for sqlglot's `qualify`. Upstream built
        it from the column frames on every query."""
        out: dict[str, dict[str, str]] = {}
        for schema in self.schemas.values():
            for name, table in schema.tables.items():
                cols = out.setdefault(name, {})
                for col in table.columns:
                    cols[col] = "TEXT"
        return out

    def table(self, schema: str | None, name: str) -> TableEntry | None:
        """The table `name` (lower-cased) in `schema` when the catalog has that schema, otherwise
        in the first schema holding it."""
        named = self.schemas.get(schema) if schema is not None else None
        if named is not None:
            return named.tables.get(name)
        for entry in self.schemas.values():
            if name in entry.tables:
                return entry.tables[name]
        return None
