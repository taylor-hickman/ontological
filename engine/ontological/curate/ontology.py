"""What the Terms and Ontology screens read (DESIGN.md §5.3, §5.5; BUILD_PLAN.md M5.2).

- **The glossary** is the Terms that represent a table of one database, each with its kind, its
  certified fields, synonyms, attribute counts and when it last changed (`glossary`). A Term is
  certified when its name and its description both are, as hybrid search counts it (M5.1).
- **A Term** adds its tables, its attributes, the join columns that lead from its tables to other
  Terms, its SQL attributes with how many logged queries use each, and its history from
  ops.change_log (`term`).
- **The graph** joins two tables where a column of one has a semantic FK, one used in join paths,
  to an attribute on the other: the edges join paths walk (ARCHITECTURE.md §1.1). The data layer
  draws the tables with those edges and the declared FKs. The semantic layer draws the Terms: two
  Terms are joined when their tables are, and a table that represents no Term, such as a
  two-key junction, bridges each pair of its neighbours as an edge `via` that table (`graph`).
- **The join path** between two Terms is a shortest path over the semantic layer's edges, so
  each hop is a join the graph draws, and its tables are what the data layer highlights (`path`).

Every call blocks and needs the DAL connected; async callers run them in a thread.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from auto_ontology.dal.session import store
from ontological.curate.edits import Change, created, history

Layer = Literal["semantic", "data"]
TermKind = Literal["entity", "association"]


class NotFound(Exception):
    """No such source or Term."""


# --- what the API returns ---------------------------------------------------------------------


class TermRef(BaseModel):
    id: str
    name: str


class TermRow(BaseModel):
    """One row of the glossary."""

    id: str
    name: str
    kind: TermKind  # an association when a table it represents is a junction table
    description: str | None
    synonyms: list[str]
    name_certified: bool
    description_certified: bool
    certified: bool  # name and description
    tables: list[str]
    attributes: int
    sql_attributes: int
    changed_at: datetime | None  # the last logged edit, else when compile wrote it


class Glossary(BaseModel):
    source_id: str
    database_name: str
    terms: list[TermRow]
    certified: int  # Terms with name and description certified
    total: int


class TableRef(BaseModel):
    id: str
    name: str
    schema_name: str
    database_name: str


class AttributeOut(BaseModel):
    id: str
    name: str
    column: str
    table: str
    datatype: str | None
    description: str | None
    certified: bool
    joins_to: TermRef | None  # the Term its column's semantic FK points at


class JoinOut(BaseModel):
    """A column of one of the Term's tables that joins to another Term, attribute or not."""

    table: str
    column: str
    to: TermRef


class SqlAttributeOut(BaseModel):
    id: str
    name: str
    description: str | None
    expression: str | None
    source: str | None  # manual, sql (query history), table, bridgeTable
    certified: bool
    uses: int  # logged queries it was derived from


class ChangeOut(BaseModel):
    id: int
    edit: int
    ts: datetime
    field: str
    old: Any
    new: Any
    actor: str
    undoes: int | None

    @classmethod
    def of(cls, change: Change) -> ChangeOut:
        return cls(
            id=change.id,
            edit=change.edit,
            ts=change.ts,
            field=change.field,
            old=change.old,
            new=change.new,
            actor=change.actor,
            undoes=change.undoes,
        )


class TermDetail(BaseModel):
    id: str
    name: str
    kind: TermKind
    description: str | None
    synonyms: list[str]
    name_certified: bool
    description_certified: bool
    created: datetime | None
    tables: list[TableRef]
    attributes: list[AttributeOut]
    joins: list[JoinOut]
    sql_attributes: list[SqlAttributeOut]
    history: list[ChangeOut]  # newest first
    undoable: int | None  # the edit Undo would revert: the latest, unless it created the Term


class GraphNode(BaseModel):
    id: str  # a Term's id on the semantic layer, a table's on the data layer
    name: str
    certified: bool  # the Term is; a table when every Term it represents is
    terms: list[TermRef]  # on the data layer, the Terms the table represents


class GraphEdge(BaseModel):
    source: str
    target: str
    via: str | None  # the junction table a semantic edge passes through
    join: str  # how the tables join, such as "Invoice.CustomerId = Customer.CustomerId"


class Graph(BaseModel):
    layer: Layer
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class PathHop(BaseModel):
    source: str  # Term ids
    target: str
    via: str | None
    join: str
    tables: list[str]  # the table ids the hop joins, junction included


class JoinPath(BaseModel):
    terms: list[TermRef]  # in order; empty when nothing joins the two
    hops: list[PathHop]
    tables: list[str]  # every table on the path, for the data layer


# --- the glossary -----------------------------------------------------------------------------


def database_of(source_id: str) -> str:
    rows = store().query_read("SELECT name FROM catalog_database WHERE id = :id", {"id": source_id})
    if not rows:
        raise NotFound(f"no source {source_id}")
    return str(rows[0]["name"])


_DB_TABLES = (
    "catalog_table t JOIN catalog_schema sc ON sc.id = t.schema_id "
    "JOIN catalog_database d ON d.id = sc.database_id"
)


def glossary(source_id: str) -> Glossary:
    database_name = database_of(source_id)
    rows = store().query_read(
        "SELECT term.id, term.name, term.description, coalesce(term.synonyms, '{}') AS synonyms, "
        "term.name_certified, term.description_certified, "
        "bool_or(t.is_junction_table) AS association, "
        "array_agg(DISTINCT t.name ORDER BY t.name) AS tables, "
        "(SELECT count(*) FROM column_attribute__term at WHERE at.term_id = term.id) "
        "  AS attributes, "
        "(SELECT count(*) FROM sql_attribute__term st WHERE st.term_id = term.id) "
        "  AS sql_attributes, "
        "coalesce((SELECT max(ts) FROM ops.change_log c WHERE c.entity = 'term' "
        "  AND c.entity_id = term.id), term.modified, term.created) AS changed_at "
        f"FROM term JOIN table__term tt ON tt.term_id = term.id JOIN {_DB_TABLES} "
        "ON t.id = tt.table_id WHERE d.id = :db "
        "GROUP BY term.id ORDER BY lower(term.name), term.id",
        {"db": source_id},
    )
    terms = [
        TermRow(
            id=r["id"],
            name=r["name"],
            kind="association" if r["association"] else "entity",
            description=r["description"],
            synonyms=list(r["synonyms"]),
            name_certified=r["name_certified"],
            description_certified=r["description_certified"],
            certified=r["name_certified"] and r["description_certified"],
            tables=list(r["tables"]),
            attributes=int(r["attributes"]),
            sql_attributes=int(r["sql_attributes"]),
            changed_at=r["changed_at"],
        )
        for r in rows
    ]
    return Glossary(
        source_id=source_id,
        database_name=database_name,
        terms=terms,
        certified=sum(t.certified for t in terms),
        total=len(terms),
    )


# --- one Term ---------------------------------------------------------------------------------


def term(term_id: str) -> TermDetail:
    rows = store().query_read(
        "SELECT id, name, description, coalesce(synonyms, '{}') AS synonyms, name_certified, "
        "description_certified, created FROM term WHERE id = :id",
        {"id": term_id},
    )
    if not rows:
        raise NotFound(f"no Term {term_id}")
    row = rows[0]
    tables = store().query_read(
        "SELECT t.id, t.name, sc.name AS schema_name, d.name AS database_name, "
        f"t.is_junction_table FROM table__term tt JOIN {_DB_TABLES} ON t.id = tt.table_id "
        "WHERE tt.term_id = :id ORDER BY t.name, t.id",
        {"id": term_id},
    )
    attributes = store().query_read(
        "SELECT a.id, a.name, a.source_column, coalesce(t.name, '') AS table_name, a.datatype, "
        "a.description, a.certified, "
        "(SELECT to_term.id FROM column__has_attribute h "
        "  JOIN column__semantic_fk f ON f.column_id = h.column_id "
        "  JOIN column_attribute__term fat ON fat.attribute_id = f.attribute_id "
        "  JOIN term to_term ON to_term.id = fat.term_id "
        "  WHERE h.attribute_id = a.id AND to_term.id <> :id "
        "  ORDER BY to_term.name LIMIT 1) AS joins_to "
        "FROM column_attribute a JOIN column_attribute__term at ON at.attribute_id = a.id "
        "LEFT JOIN catalog_table t ON t.id = a.table_id "
        "WHERE at.term_id = :id ORDER BY t.name, a.name, a.id",
        {"id": term_id},
    )
    joins = store().query_read(
        "SELECT DISTINCT t.name AS table_name, c.name AS column_name, to_term.id AS to_id, "
        "to_term.name AS to_name FROM table__term tt "
        "JOIN catalog_table t ON t.id = tt.table_id "
        "JOIN catalog_column c ON c.table_id = t.id "
        "JOIN column__semantic_fk f ON f.column_id = c.id AND f.use_in_join_paths "
        "JOIN column_attribute__term fat ON fat.attribute_id = f.attribute_id "
        "JOIN term to_term ON to_term.id = fat.term_id "
        "WHERE tt.term_id = :id AND to_term.id <> :id "
        "ORDER BY t.name, c.name, to_term.name",
        {"id": term_id},
    )
    sql_attributes = store().query_read(
        "SELECT s.id, s.name, s.description, s.expression, s.source, s.certified, "
        "(SELECT count(*) FROM sql_attribute__sql q WHERE q.attribute_id = s.id) AS uses "
        "FROM sql_attribute s JOIN sql_attribute__term st ON st.attribute_id = s.id "
        "WHERE st.term_id = :id ORDER BY s.name",
        {"id": term_id},
    )
    names = _term_names({r["joins_to"] for r in attributes if r["joins_to"]})
    changes = history("term", term_id)
    return TermDetail(
        id=row["id"],
        name=row["name"],
        kind="association" if any(t["is_junction_table"] for t in tables) else "entity",
        description=row["description"],
        synonyms=list(row["synonyms"]),
        name_certified=row["name_certified"],
        description_certified=row["description_certified"],
        created=row["created"],
        tables=[
            TableRef(
                id=t["id"],
                name=t["name"],
                schema_name=t["schema_name"],
                database_name=t["database_name"],
            )
            for t in tables
        ],
        attributes=[
            AttributeOut(
                id=a["id"],
                name=a["name"],
                column=a["source_column"],
                table=a["table_name"],
                datatype=a["datatype"],
                description=a["description"],
                certified=a["certified"],
                joins_to=(
                    TermRef(id=a["joins_to"], name=names[a["joins_to"]])
                    if a["joins_to"] in names
                    else None
                ),
            )
            for a in attributes
        ],
        joins=[
            JoinOut(
                table=j["table_name"],
                column=j["column_name"],
                to=TermRef(id=j["to_id"], name=j["to_name"]),
            )
            for j in joins
        ],
        sql_attributes=[
            SqlAttributeOut(
                id=s["id"],
                name=s["name"],
                description=s["description"],
                expression=s["expression"],
                source=s["source"],
                certified=s["certified"],
                uses=int(s["uses"]),
            )
            for s in sql_attributes
        ],
        history=[ChangeOut.of(c) for c in changes],
        undoable=_undoable(changes),
    )


def _undoable(changes: list[Change]) -> int | None:
    """The edit Undo reverts, the latest, unless it created the Term."""
    if not changes:
        return None
    latest = changes[0].edit
    return None if any(created(c) for c in changes if c.edit == latest) else latest


def _term_names(ids: Iterable[str]) -> dict[str, str]:
    wanted = sorted(ids)
    if not wanted:
        return {}
    rows = store().query_read("SELECT id, name FROM term WHERE id = ANY(:ids)", {"ids": wanted})
    return {r["id"]: r["name"] for r in rows}


# --- the graph --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Table:
    id: str
    name: str
    junction: bool


@dataclass(frozen=True)
class _Join:
    """A join between two tables: `a.column = b.column`."""

    a: str
    b: str
    label: str


@dataclass(frozen=True)
class _TermEdge:
    a: str
    b: str
    via: str | None
    join: str
    tables: tuple[str, ...]


@dataclass
class _Layers:
    tables: dict[str, _Table]
    terms_of: dict[str, list[str]]  # table id -> Term ids
    terms: dict[str, tuple[str, bool]]  # Term id -> name, certified
    semantic_joins: list[_Join]  # semantic FKs used in join paths
    declared_joins: list[_Join]  # declared FKs, for the data layer

    def term_edges(self) -> list[_TermEdge]:
        """One edge per pair of Terms whose tables join, directly or through a table that
        represents no Term. The first join by name labels it."""
        by_table: dict[str, list[tuple[str, _Join]]] = {}
        for j in self.semantic_joins:
            by_table.setdefault(j.a, []).append((j.b, j))
            by_table.setdefault(j.b, []).append((j.a, j))
        edges: dict[tuple[str, str], _TermEdge] = {}

        def add(ta: str, tb: str, via: str | None, join: str, tables: tuple[str, ...]) -> None:
            for x in self.terms_of.get(ta, []):
                for y in self.terms_of.get(tb, []):
                    if x == y:
                        continue
                    key = (x, y) if x < y else (y, x)
                    if key not in edges:
                        edges[key] = _TermEdge(key[0], key[1], via, join, tables)

        for j in sorted(self.semantic_joins, key=lambda j: j.label):
            add(j.a, j.b, None, j.label, (j.a, j.b))
        for bridge, joined in sorted(by_table.items(), key=lambda kv: self.tables[kv[0]].name):
            if self.terms_of.get(bridge):
                continue
            ends = sorted(joined, key=lambda nj: nj[1].label)
            for i, (ta, ja) in enumerate(ends):
                for tb, jb in ends[i + 1 :]:
                    if ta != tb:
                        label = f"{ja.label}; {jb.label}"
                        add(ta, tb, self.tables[bridge].name, label, (ta, bridge, tb))
        return sorted(edges.values(), key=lambda e: (e.a, e.b))


def _layers(source_id: str) -> _Layers:
    database_of(source_id)
    tables = {
        r["id"]: _Table(r["id"], r["name"], r["is_junction_table"])
        for r in store().query_read(
            f"SELECT t.id, t.name, t.is_junction_table FROM {_DB_TABLES} WHERE d.id = :db",
            {"db": source_id},
        )
    }
    terms_of: dict[str, list[str]] = {}
    terms: dict[str, tuple[str, bool]] = {}
    for r in store().query_read(
        "SELECT tt.table_id, term.id, term.name, "
        "term.name_certified AND term.description_certified AS certified "
        f"FROM table__term tt JOIN term ON term.id = tt.term_id JOIN {_DB_TABLES} "
        "ON t.id = tt.table_id WHERE d.id = :db ORDER BY lower(term.name), term.id",
        {"db": source_id},
    ):
        terms_of.setdefault(r["table_id"], []).append(r["id"])
        terms[r["id"]] = (r["name"], r["certified"])
    semantic = store().query_read(
        "SELECT DISTINCT sc.table_id AS a, st.name AS a_name, sc.name AS a_column, "
        "tc.table_id AS b, tt.name AS b_name, tc.name AS b_column "
        "FROM column__semantic_fk f "
        "JOIN catalog_column sc ON sc.id = f.column_id "
        "JOIN catalog_table st ON st.id = sc.table_id "
        "JOIN column__has_attribute h ON h.attribute_id = f.attribute_id "
        "JOIN catalog_column tc ON tc.id = h.column_id "
        "JOIN catalog_table tt ON tt.id = tc.table_id "
        "WHERE f.use_in_join_paths AND sc.table_id = ANY(:tables) AND tc.table_id = ANY(:tables)",
        {"tables": list(tables)},
    )
    declared = store().query_read(
        "SELECT DISTINCT sc.table_id AS a, st.name AS a_name, sc.name AS a_column, "
        "tc.table_id AS b, tt.name AS b_name, tc.name AS b_column "
        "FROM column__foreign_key k "
        "JOIN catalog_column sc ON sc.id = k.source_column_id "
        "JOIN catalog_table st ON st.id = sc.table_id "
        "JOIN catalog_column tc ON tc.id = k.target_column_id "
        "JOIN catalog_table tt ON tt.id = tc.table_id "
        "WHERE sc.table_id = ANY(:tables) AND tc.table_id = ANY(:tables)",
        {"tables": list(tables)},
    )
    return _Layers(tables, terms_of, terms, _joins(semantic), _joins(declared))


def _joins(rows: list[dict[str, Any]]) -> list[_Join]:
    """Joins between two different tables; a self-reference joins nothing to draw."""
    return [
        _Join(r["a"], r["b"], f"{r['a_name']}.{r['a_column']} = {r['b_name']}.{r['b_column']}")
        for r in rows
        if r["a"] != r["b"]
    ]


def graph(source_id: str, layer: Layer) -> Graph:
    layers = _layers(source_id)
    if layer == "semantic":
        nodes = [
            GraphNode(id=term_id, name=name, certified=certified, terms=[])
            for term_id, (name, certified) in layers.terms.items()
        ]
        edges = [
            GraphEdge(source=e.a, target=e.b, via=e.via, join=e.join) for e in layers.term_edges()
        ]
        return Graph(layer=layer, nodes=_by_name(nodes), edges=edges)
    nodes: list[GraphNode] = []
    for table in layers.tables.values():
        term_ids = layers.terms_of.get(table.id, [])
        nodes.append(
            GraphNode(
                id=table.id,
                name=table.name,
                certified=bool(term_ids) and all(layers.terms[t][1] for t in term_ids),
                terms=[TermRef(id=t, name=layers.terms[t][0]) for t in term_ids],
            )
        )
    seen: dict[tuple[str, str], GraphEdge] = {}
    for j in sorted(layers.semantic_joins + layers.declared_joins, key=lambda j: j.label):
        key = (j.a, j.b) if j.a < j.b else (j.b, j.a)
        if key not in seen:
            seen[key] = GraphEdge(source=key[0], target=key[1], via=None, join=j.label)
    return Graph(layer=layer, nodes=_by_name(nodes), edges=sorted(seen.values(), key=_edge_key))


def _by_name(nodes: list[GraphNode]) -> list[GraphNode]:
    return sorted(nodes, key=lambda n: (n.name.lower(), n.id))


def _edge_key(e: GraphEdge) -> tuple[str, str]:
    return (e.source, e.target)


# --- the join path ----------------------------------------------------------------------------


def path(source_id: str, from_term: str, to_term: str) -> JoinPath:
    """A shortest join path between two Terms of the source, over the semantic layer's edges.
    Ties go to the neighbour whose name sorts first, so the same graph gives the same path."""
    layers = _layers(source_id)
    for t in (from_term, to_term):
        if t not in layers.terms:
            raise NotFound(f"no Term {t} in this source")
    nothing = JoinPath(terms=[], hops=[], tables=[])
    if from_term == to_term:
        name = layers.terms[from_term][0]
        tables = [t for t, ids in layers.terms_of.items() if from_term in ids]
        return JoinPath(terms=[TermRef(id=from_term, name=name)], hops=[], tables=sorted(tables))
    neighbours: dict[str, list[tuple[str, _TermEdge]]] = {}
    for e in layers.term_edges():
        neighbours.setdefault(e.a, []).append((e.b, e))
        neighbours.setdefault(e.b, []).append((e.a, e))
    for adjacent in neighbours.values():
        adjacent.sort(key=lambda ne: (layers.terms[ne[0]][0].lower(), ne[0]))
    came_from: dict[str, tuple[str, _TermEdge] | None] = {from_term: None}
    queue = deque([from_term])
    while queue and to_term not in came_from:
        here = queue.popleft()
        for there, edge in neighbours.get(here, []):
            if there not in came_from:
                came_from[there] = (here, edge)
                queue.append(there)
    if to_term not in came_from:
        return nothing
    order = [to_term]
    hops: list[PathHop] = []
    while (step := came_from[order[-1]]) is not None:
        previous, edge = step
        tables = list(edge.tables)
        if previous not in layers.terms_of.get(tables[0], []):
            tables.reverse()
        hops.append(
            PathHop(source=previous, target=order[-1], via=edge.via, join=edge.join, tables=tables)
        )
        order.append(previous)
    order.reverse()
    hops.reverse()
    on_path: list[str] = []
    for hop in hops:
        on_path += [t for t in hop.tables if t not in on_path]
    return JoinPath(
        terms=[TermRef(id=t, name=layers.terms[t][0]) for t in order],
        hops=hops,
        tables=on_path,
    )
