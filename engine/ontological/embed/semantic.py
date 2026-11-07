# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/embed.py at 2a24f5d. The
# row texts and metadata are upstream's. Upstream embeds each table's Terms through its NIM
# client as it commits them, and appends. This syncs a whole database after its compile: it
# embeds through the local embedder the rows whose text or model changed, or that have no
# vector, and deletes the vectors of rows that changed or are gone. Each row's metadata also says
# whether it is certified, for hybrid search's bonus, and a change to that alone updates the
# vector's metadata in place. SQL attributes' rows are upstream's from `fetch_sql_attribute_docs`
# and `embed_docs_into_vdb`, which upstream embedded as each attribute was created.

"""The semantic layer's vectors: one per Term, column attribute and SQL attribute
(ARCHITECTURE.md §7.1). Retrieval (`ontological.answer`) searches them.

A row's vector is current when the collection holds one for its label and id with the same
`content_hash` and `model`. `sync_database` embeds every row that has no current vector and
deletes every other vector of the database's Terms and attributes, so it can run after any
compile, finished or stopped, and catches up whatever an earlier run left out. Curation edits
(`ontological.curate`) run it too, so an edit re-embeds only the texts it changed.

Each row's metadata carries `certified`, which hybrid search's bonus reads (§7.3): a Term's when
its name and its description are both certified, an attribute's when it is. It isn't in the text,
so a certification re-embeds nothing: the sync writes the flag into the current vector's metadata.
"""

# The vendored DAL and vdb are untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy import func, select

from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store
from auto_ontology.dal.sql_attributes import fetch_sql_attribute_docs
from auto_ontology.semantic.constants import (
    LABEL_COLUMN_ATTRIBUTE,
    LABEL_SQL_ATTRIBUTE,
    LABEL_TERM,
    SEMANTIC_SOURCE,
)
from auto_ontology.utils.sample_values import stringify_sample_values
from auto_ontology.vdb.postgres import PostgresVDB
from auto_ontology.vdb.records import content_hash, embed_rows_into_vdb

logger = logging.getLogger(__name__)

LABELS = (LABEL_TERM, LABEL_COLUMN_ATTRIBUTE, LABEL_SQL_ATTRIBUTE)

# Upstream's cutoffs for one formatted sample value in an attribute's text: JSON columns' samples
# are `key [e.g. 'value']` strings, which need more room than a bare value.
_MAX_EMBEDDED_SAMPLE_LEN = 30
_MAX_EMBEDDED_JSON_SAMPLE_LEN = 60


def _format_sample_values(raw: Any, data_type: str | None = None) -> str:
    max_len = (
        _MAX_EMBEDDED_JSON_SAMPLE_LEN
        if "json" in (data_type or "").lower()
        else _MAX_EMBEDDED_SAMPLE_LEN
    )
    values = stringify_sample_values(raw, max_len=max_len)
    if not values:
        return ""
    return " Sample values: " + ", ".join(values) + "."


def build_rows(
    database_name: str, term: dict[str, Any], attrs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Upstream's rows for one Term and its attributes: `{text, path, page_number, metadata}`."""
    rows: list[dict[str, Any]] = []

    term_name = term.get("name")
    if term_name:
        text = f"Term: {term_name}. {term.get('description') or ''}".strip()
        path = f"semantic:term:{term_name}"
        fields: dict[str, Any] = {
            "label": LABEL_TERM,
            "name": term_name,
            "database_name": database_name,
            "source_path": path,
        }
        if term.get("id"):
            fields["id"] = term["id"]
        fields["certified"] = bool(term.get("name_certified") and term.get("description_certified"))
        schema_names = [n for n in (term.get("schema_names") or []) if n]
        if schema_names:
            fields["schema_names"] = schema_names
        rows.append(
            {
                "text": text,
                "path": path,
                "page_number": -1,
                "metadata": {**fields, "content_metadata": dict(fields)},
            }
        )

    term_synonyms: list[str] = term.get("synonyms") or []
    synonym_suffix = f" ({', '.join(term_synonyms)})" if term_synonyms else ""

    for a in attrs:
        attr_name = a.get("name")
        if not attr_name:
            continue
        owner = a.get("term_name") or term_name or ""
        sample_block = _format_sample_values(a.get("sample_values"), a.get("datatype"))
        text = (
            f"ColumnAttribute: {attr_name} of Term {owner}{synonym_suffix}. "
            f"{a.get('description') or ''}"
            f"{sample_block}"
        ).strip()
        path = (
            f"semantic:attr:{a['id']}"
            if a.get("id")
            else f"semantic:attr:{owner}:{a.get('source_column')}"
        )
        fields = {
            "label": LABEL_COLUMN_ATTRIBUTE,
            "name": attr_name,
            "term_name": owner,
            "source_column": a.get("source_column"),
            "table_id": a.get("table_id"),
            "table_name": a.get("table_name"),
            "is_unique": a.get("is_unique"),
            "database_name": database_name,
            "source_path": path,
        }
        if a.get("schema_name"):
            fields["schema_name"] = a["schema_name"]
        if a.get("datatype"):
            fields["data_type"] = a["datatype"]
        if a.get("id"):
            fields["id"] = a["id"]
        fields["certified"] = bool(a.get("certified"))
        rows.append(
            {
                "text": text,
                "path": path,
                "page_number": -1,
                "metadata": {**fields, "content_metadata": dict(fields)},
            }
        )

    return rows


def terms_and_attributes(
    database_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(terms, attrs)` of one database, in the shapes upstream's `fetch_all_terms_and_attributes`
    returns for every database at once."""
    in_database = (s.catalog_database.c.name == database_name,)
    tables = s.catalog_table.join(
        s.catalog_schema, s.catalog_schema.c.id == s.catalog_table.c.schema_id
    ).join(s.catalog_database, s.catalog_database.c.id == s.catalog_schema.c.database_id)
    terms = store().query_read(
        select(
            s.term.c.name,
            s.term.c.description,
            s.term.c.synonyms,
            s.term.c.id,
            s.term.c.name_certified,
            s.term.c.description_certified,
            func.array_agg(func.distinct(s.catalog_schema.c.name)).label("schema_names"),
        )
        .select_from(
            s.table__term.join(s.term, s.term.c.id == s.table__term.c.term_id).join(
                tables, s.catalog_table.c.id == s.table__term.c.table_id
            )
        )
        .where(*in_database, s.term.c.source == SEMANTIC_SOURCE)
        .group_by(s.term.c.id)
        .order_by(s.term.c.name)
    )
    attrs = store().query_read(
        select(
            s.column_attribute.c.name,
            s.column_attribute.c.description,
            s.column_attribute.c.term_name,
            s.column_attribute.c.source_column,
            s.catalog_column.c.sample_values,
            s.catalog_column.c.is_unique,
            s.catalog_table.c.id.label("table_id"),
            s.catalog_table.c.name.label("table_name"),
            s.column_attribute.c.id,
            s.catalog_schema.c.name.label("schema_name"),
            s.column_attribute.c.datatype,
            s.column_attribute.c.certified,
        )
        .select_from(
            s.column_attribute.join(
                s.column__has_attribute,
                s.column__has_attribute.c.attribute_id == s.column_attribute.c.id,
            )
            .join(s.catalog_column, s.catalog_column.c.id == s.column__has_attribute.c.column_id)
            .join(tables, s.catalog_table.c.id == s.catalog_column.c.table_id)
        )
        .where(*in_database, s.column_attribute.c.source == SEMANTIC_SOURCE)
        .order_by(s.catalog_table.c.name, s.catalog_column.c.ordinal_position)
    )
    return [dict(r) for r in terms], [dict(r) for r in attrs]


def sql_attribute_rows(database_name: str) -> list[dict[str, Any]]:
    """A row per SQL attribute of the database's Terms, with upstream's text
    (`fetch_sql_attribute_docs`) and the metadata upstream's `embed_docs_into_vdb` gave it."""
    tables = s.catalog_table.join(
        s.catalog_schema, s.catalog_schema.c.id == s.catalog_table.c.schema_id
    ).join(s.catalog_database, s.catalog_database.c.id == s.catalog_schema.c.database_id)
    ids = store().query_read(
        select(s.sql_attribute__term.c.attribute_id)
        .distinct()
        .select_from(
            s.sql_attribute__term.join(
                s.table__term, s.table__term.c.term_id == s.sql_attribute__term.c.term_id
            ).join(tables, s.catalog_table.c.id == s.table__term.c.table_id)
        )
        .where(s.catalog_database.c.name == database_name)
        .order_by(s.sql_attribute__term.c.attribute_id)
    )
    rows: list[dict[str, Any]] = []
    for r in ids:
        for doc in fetch_sql_attribute_docs(r["attribute_id"]):
            path = f"auto_ontology:{doc['id']}"
            fields = {
                "id": doc["id"],
                "label": doc["label"],
                "name": doc["name"],
                "source_path": path,
                "database_name": database_name,
                "source": doc.get("source", ""),
                "certified": False,  # nothing certifies a SQL attribute yet
            }
            rows.append(
                {
                    "text": (doc.get("text") or "").strip(),
                    "path": path,
                    "page_number": -1,
                    "metadata": {**fields, "content_metadata": dict(fields)},
                }
            )
    return rows


def database_rows(database_name: str) -> list[dict[str, Any]]:
    """Every Term's row, its attributes' rows and its SQL attributes' rows, for one database."""
    terms, attrs = terms_and_attributes(database_name)
    attrs_by_term: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attr in attrs:
        if attr.get("term_name"):
            attrs_by_term[attr["term_name"]].append(attr)
    return [
        row
        for term in terms
        if term.get("name")
        for row in build_rows(database_name, term, attrs_by_term.get(term["name"], []))
    ] + sql_attribute_rows(database_name)


@dataclass(frozen=True)
class Synced:
    embedded: int  # rows embedded and written
    deleted: int  # vectors deleted: their row changed, or is gone
    current: int  # rows whose vector was already current
    retagged: int = 0  # current vectors whose `certified` flag was rewritten in place


Key = tuple[str, str, str, str]  # label, id, content_hash, model


def _key(metadata: dict[str, Any], text_hash: str, model: str) -> Key:
    return (str(metadata.get("label")), str(metadata.get("id")), text_hash, model)


def sync_database(vdb: PostgresVDB, database_name: str) -> Synced:
    """Make the vectors of `database_name`'s Terms and attributes current, embedding with the
    VDB's embedder."""
    rows = database_rows(database_name)
    model = str(getattr(vdb.embeddings, "model", None) or type(vdb.embeddings).__name__)
    wanted = {_key(row["metadata"], content_hash(row["text"]), model): row for row in rows}

    stored = vdb.row_metadata({"database_name": database_name, "label": {"$in": list(LABELS)}})
    kept: set[Key] = set()
    stale: list[str] = []
    retag: list[tuple[str, bool]] = []
    for row_id, metadata in stored:
        key = _key(metadata, str(metadata.get("content_hash")), str(metadata.get("model")))
        if key in wanted and key not in kept:
            kept.add(key)
            certified = wanted[key]["metadata"]["certified"]
            if metadata.get("certified") is not certified:
                retag.append((row_id, certified))
        else:
            stale.append(row_id)
    vdb.delete_rows(stale)
    _retag(vdb, retag)

    missing = [row for key, row in wanted.items() if key not in kept]
    embedded = (
        embed_rows_into_vdb(missing, vdb.embeddings, vdb, what="semantic layer") if missing else 0
    )
    logger.info(
        "semantic layer of %s: %d embedded, %d deleted, %d current, %d retagged",
        database_name,
        embedded,
        len(stale),
        len(kept),
        len(retag),
    )
    return Synced(embedded=embedded, deleted=len(stale), current=len(kept), retagged=len(retag))


def _retag(vdb: PostgresVDB, rows: list[tuple[str, bool]]) -> None:
    """Set `certified` in these rows' metadata (a `json` column), and in the copy under
    `content_metadata`. The text is unchanged, so BM25's index is too, and hybrid search reads
    the flag from the row."""
    if not rows:
        return
    statement = sql.SQL(
        "UPDATE {} SET langchain_metadata = jsonb_set("
        "langchain_metadata::jsonb || %(patch)s::jsonb, '{{content_metadata}}', "
        "coalesce(langchain_metadata::jsonb -> 'content_metadata', '{{}}') || %(patch)s::jsonb"
        ")::json WHERE langchain_id = %(id)s::uuid"
    ).format(sql.Identifier(vdb.schema_name, vdb.collection_name))
    with psycopg.connect(vdb.connection_string) as conn, conn.cursor() as cur:
        cur.executemany(
            statement,
            [{"id": row_id, "patch": json.dumps({"certified": c})} for row_id, c in rows],
        )
