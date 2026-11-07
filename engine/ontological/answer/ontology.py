"""What the answer agent's tools read from the store (`ontological.answer.tools.Ontology`): a
database's catalog for the gate, its tables' key columns and semantic-FK graph, search over the
semantic layer, `plan`'s grounding pack for its entities, a Term's and a table's detail, and the
join path between two tables. Everything goes through the vendored DAL and the semantic layer's
vector store, so the DAL must be connected (`ontological.store.dal.connect`). Search is whichever
the caller passes: the engine's is hybrid search (`ontological.embed.hybrid.searcher`)."""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, or_, select

from auto_ontology.dal import schema as s
from auto_ontology.dal.attributes import (
    column_participates_in_semantic_fk,
    find_join_path,
    find_shared_hub_bridge,
)
from auto_ontology.dal.datasources import (
    fetch_tables_by_ids,
    find_column_id_by_table_and_name,
    find_table_id_by_name,
    find_table_key_columns,
)
from auto_ontology.dal.session import store
from auto_ontology.dal.sql_attributes import fetch_sql_attributes_by_term_id
from auto_ontology.dal.terms import fetch_column_attributes_by_term_id, get_full_term_by_id
from auto_ontology.semantic.constants import LABEL_COLUMN_ATTRIBUTE, LABEL_TERM
from auto_ontology.vdb.postgres import PostgresVDB
from ontological.answer.gate import KeyColumns
from ontological.answer.pack import GroundingPack, build_pack
from ontological.answer.retrieval import Search, search_by_label
from ontological.catalog.parsers.catalog_index import CatalogIndex
from ontological.catalog.query_history import read_catalog_index
from ontological.embed.search import dense_search

SEARCH_K = 8  # hits per kind
_LABELS = {"attribute": LABEL_COLUMN_ATTRIBUTE, "term": LABEL_TERM}


class StoreOntology:
    def __init__(self, vdb: PostgresVDB, search: Search = dense_search) -> None:
        self.vdb = vdb
        self._search = search

    def catalog(self, database: str) -> CatalogIndex:
        return read_catalog_index(database)

    def key_columns(self, database: str, table: str) -> KeyColumns:
        return _key_columns(database, table)

    def join_graph(self, database: str) -> StoreJoinGraph:
        return StoreJoinGraph(database)

    def search(self, database: str, query: str, kinds: Sequence[str]) -> list[dict[str, Any]]:
        """The nearest attributes and Terms, nearest first across kinds."""
        hits: list[dict[str, Any]] = []
        for kind in kinds:
            hits += search_by_label(
                self.vdb, query, _LABELS[kind], SEARCH_K, database, self._search
            )
        return sorted(hits, key=lambda h: h["score"])

    def plan_pack(
        self,
        database: str,
        question: str,
        *,
        entities: Sequence[str],
        evidence: str,
        subject: str,
        anchor_attribute_id: str | None,
    ) -> GroundingPack:
        """Upstream's per-entity retrieval in `database`, with the join paths from the anchor."""
        return build_pack(
            self.vdb,
            question,
            entities=entities,
            evidence=evidence,
            subject=subject,
            database=database,
            anchor_attribute_id=anchor_attribute_id,
            search=self._search,
        )

    def get_term(self, database: str, term: str) -> dict[str, Any] | None:
        """The Term with that id, or else that name, among `database`'s: its description,
        synonyms, tables, attributes and SQL attributes."""
        term_id = _term_id(database, term)
        found = get_full_term_by_id(term_id) if term_id else None
        if found is None or term_id is None:
            return None
        attributes = fetch_column_attributes_by_term_id(term_id)
        sql_attributes = fetch_sql_attributes_by_term_id(term_id)
        return {
            "id": found["id"],
            "name": found["name"],
            "description": found.get("description") or "",
            "synonyms": list(found.get("synonyms") or []),
            "tables": [t["name"] for t in found.get("tables") or []],
            "attributes": [
                {
                    "id": a["id"],
                    "name": a["name"],
                    "description": a.get("description") or "",
                    "column": _column(a.get("primary_column")),
                    "datatype": a.get("datatype") or "",
                }
                for a in attributes
            ],
            "sql_attributes": [
                {
                    "id": a["id"],
                    "name": a["name"],
                    "description": a.get("description") or "",
                    "sql": a.get("sql") or a.get("expression") or "",
                }
                for a in sql_attributes
            ],
        }

    def join_path(
        self, database: str, from_table: str, to_table: str
    ) -> list[dict[str, str]] | None:
        """The semantic-FK hops from one table to the other, tried both ways as upstream's
        `find_table_bridge` does, since the traversal isn't symmetric; None when either table
        isn't in `database`."""
        start, end = _any_column(database, from_table), _any_column(database, to_table)
        if start is None or end is None:
            return None
        hops: list[dict[str, str]] = find_join_path(start, end)
        if hops:
            return hops
        back: list[dict[str, str]] = find_join_path(end, start)
        return [_reversed(h) for h in reversed(back)]

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


class StoreJoinGraph:
    """`database`'s semantic-FK graph, for the gate's join check (`join_check.JoinGraph`)."""

    def __init__(self, database: str) -> None:
        self.database = database

    def column_id(self, table: str, column: str) -> str | None:
        return find_column_id_by_table_and_name(table, column, self.database)

    def join_path(self, col_a_id: str, col_b_id: str) -> list[dict[str, Any]]:
        return find_join_path(col_a_id, col_b_id)

    def in_semantic_fk(self, col_id: str) -> bool:
        return column_participates_in_semantic_fk(col_id)

    def shared_hub(self, col_a_id: str, col_b_id: str) -> dict[str, Any]:
        return find_shared_hub_bridge(col_a_id, col_b_id)

    def key_columns(self, table: str) -> KeyColumns:
        return _key_columns(self.database, table)


def _term_id(database: str, term: str) -> str | None:
    """The id of the Term of `database` with id `term`, or else named `term` (any case)."""
    tables = (
        s.table__term.join(s.catalog_table, s.catalog_table.c.id == s.table__term.c.table_id)
        .join(s.catalog_schema, s.catalog_schema.c.id == s.catalog_table.c.schema_id)
        .join(s.catalog_database, s.catalog_database.c.id == s.catalog_schema.c.database_id)
        .join(s.term, s.term.c.id == s.table__term.c.term_id)
    )
    rows = store().query_read(
        select(s.term.c.id, s.term.c.name)
        .distinct()
        .select_from(tables)
        .where(
            s.catalog_database.c.name == database,
            or_(s.term.c.id == term, func.lower(s.term.c.name) == term.strip().lower()),
        )
        .order_by(s.term.c.id)
    )
    ids = [str(r["id"]) for r in rows]
    return term if term in ids else next(iter(ids), None)


def _any_column(database: str, table: str) -> str | None:
    """A column of the table with that id, or else that name, in `database`: `find_join_path`
    takes columns, and any column of a table stands for it."""
    for table_id in (table, find_table_id_by_name(table, database)):
        if not table_id:
            continue
        for found in fetch_tables_by_ids([table_id]):
            if found.get("database_name") == database and found.get("columns"):
                column = found["columns"][0]["name"]
                return find_column_id_by_table_and_name(found["name"], column, database)
    return None


def _reversed(hop: dict[str, str]) -> dict[str, str]:
    """A hop walked the other way: its target becomes its source."""
    swapped = {}
    for key, value in hop.items():
        if key.startswith("source_"):
            swapped["target_" + key.removeprefix("source_")] = value
        elif key.startswith("target_"):
            swapped["source_" + key.removeprefix("target_")] = value
        else:
            swapped[key] = value
    return swapped


def _column(place: dict[str, Any] | None) -> str:
    return f"{place['table_name']}.{place['column_name']}" if place else ""


def _key_columns(database: str, table: str) -> KeyColumns:
    keys = find_table_key_columns(table, database)
    return KeyColumns(pk=list(keys["pk"]), unique=list(keys["unique"]))
