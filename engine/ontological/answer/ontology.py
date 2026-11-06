"""What the answer agent's tools read from the store (`ontological.answer.tools.Ontology`): a
database's catalog for the gate, its tables' key columns, dense search over the semantic layer,
and a table's detail. Everything goes through the vendored DAL and the semantic layer's vector
store, so the DAL must be connected (`ontological.store.dal.connect`)."""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from auto_ontology.dal.datasources import (
    fetch_tables_by_ids,
    find_table_id_by_name,
    find_table_key_columns,
)
from auto_ontology.semantic.constants import LABEL_COLUMN_ATTRIBUTE, LABEL_TERM
from auto_ontology.vdb.postgres import PostgresVDB
from ontological.answer.gate import KeyColumns
from ontological.answer.retrieval import search_by_label
from ontological.catalog.parsers.catalog_index import CatalogIndex
from ontological.catalog.query_history import read_catalog_index

SEARCH_K = 8  # hits per kind
_LABELS = {"attribute": LABEL_COLUMN_ATTRIBUTE, "term": LABEL_TERM}


class StoreOntology:
    def __init__(self, vdb: PostgresVDB) -> None:
        self.vdb = vdb

    def catalog(self, database: str) -> CatalogIndex:
        return read_catalog_index(database)

    def key_columns(self, database: str, table: str) -> KeyColumns:
        keys = find_table_key_columns(table, database)
        return KeyColumns(pk=list(keys["pk"]), unique=list(keys["unique"]))

    def search(self, database: str, query: str, kinds: Sequence[str]) -> list[dict[str, Any]]:
        """The nearest attributes and Terms, nearest first across kinds."""
        hits: list[dict[str, Any]] = []
        for kind in kinds:
            hits += search_by_label(self.vdb, query, _LABELS[kind], SEARCH_K, database)
        return sorted(hits, key=lambda h: h["score"])

    def describe_table(self, database: str, table: str) -> dict[str, Any] | None:
        """The table with that id, or else that name, in `database`."""
        for table_id in (table, find_table_id_by_name(table, database)):
            if not table_id:
                continue
            found = [
                t for t in fetch_tables_by_ids([table_id]) if t.get("database_name") == database
            ]
            if found:
                return found[0]
        return None
