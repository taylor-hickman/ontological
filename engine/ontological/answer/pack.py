# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from the deterministic parts of upstream's
# auto_ontology/retrieval/text_to_sql/agents/candidates_preparation.py at 2a24f5d: the attributes'
# store context, their Terms' synonyms, the tables they and the subject Term live in, and the
# join paths from the anchor attribute (dal/attributes.py::find_join_path). Upstream's LLM steps
# are dropped: the anchor is the caller's (the agent's `plan`, ARCHITECTURE.md §8.1) or else the
# nearest attribute, where upstream asks its LLM, and no relevance filter drops tables. Junction
# and bridge-table expansion isn't ported yet; SQL attributes and custom analyses join with the
# stages that write them (M3.5, M5.3).

"""The grounding pack (ARCHITECTURE.md §8.1): what the answer agent is shown about a question
before it starts, and what `plan` returns for the entities the agent extracted.

Built in Python with no Claude call: dense retrieval over the semantic layer's Terms and
attributes (`ontological.answer.retrieval`), the store's context for each attribute, the tables
they belong to, and the shortest semantic-FK join path, by BFS, from the anchor attribute's table
to each other attribute's.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from auto_ontology.dal.attributes import fetch_attr_column_contexts, find_join_path
from auto_ontology.dal.datasources import fetch_tables_by_ids
from auto_ontology.dal.terms import fetch_term_synonyms, fetch_term_table_pairs
from auto_ontology.vdb.postgres import PostgresVDB
from ontological.answer.retrieval import (
    ENTITY_K,
    SUBJECT_K,
    Coverage,
    Hit,
    Search,
    retrieve,
)
from ontological.embed.search import dense_search

logger = logging.getLogger(__name__)

# Before the agent extracts entities, the raw question is the one entity. Upstream took its
# per-label default, 10. The retrieval benchmark (M3.9, BUILD_PLAN.md) put it at 30, where recall
# levels off: on dev, hybrid search with the default model finds 0.94 of the gold tables and 0.81
# of the gold columns at 30, against 0.80 and 0.56 at 10, for a pack of about 9 tables rather
# than 6. M4.7 checks it against answer accuracy.
QUESTION_K = 30

# The context fields an attribute and its join-path entry carry (upstream's
# `primary_attribute` and `attribute_join_paths`).
_PLACE = ("attr_name", "col_name", "table_name", "schema_name", "database_name")


@dataclass(frozen=True)
class GroundingPack:
    question: str
    database: str | None  # the scope given, or the one with the best coverage
    # Each attribute hit, nearest first, with its store context: `attr_name`,
    # `attr_description`, `col_id`, `col_name`, `table_id`, `table_name`, `schema_name`,
    # `database_name`, `term_name`, `datatype`
    attributes: list[dict[str, Any]]
    terms: list[Hit]  # the subject's Terms
    term_synonyms: dict[str, list[str]]  # by Term name, for the attributes' Terms
    # The attributes' tables, nearest first, then the subject Terms' tables, with their columns
    # (`fetch_tables_by_ids`)
    tables: list[dict[str, Any]]
    anchor: dict[str, Any] | None  # upstream's `primary_attribute`
    # Upstream's `attribute_join_paths`: per other attribute, its place and `path`, the hops
    # from the anchor's table to its own (`find_join_path`; [] in the same table or unreachable)
    join_paths: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    coverage: list[Coverage] = field(default_factory=list[Coverage])  # when no scope was given


def build_pack(
    vdb: PostgresVDB,
    question: str,
    *,
    entities: Sequence[str] | None = None,
    evidence: str = "",
    subject: str | None = None,
    database: str | None = None,
    anchor_attribute_id: str | None = None,
    question_k: int = QUESTION_K,
    search: Search = dense_search,
) -> GroundingPack:
    """The grounding pack for `question`, within `database` when it is given.

    Without `entities`, the raw question searches the attributes (`question_k` of them) and the
    Terms, as the agent's pre-work. With them, each entity searches the attributes and `subject`
    the Terms, with upstream's k values, as `plan` does. Join paths start at
    `anchor_attribute_id` when it is among the attributes, otherwise at the nearest one."""
    if entities is None:
        found = retrieve(
            vdb,
            [question],
            evidence=evidence,
            subject=question if subject is None else subject,
            database=database,
            entity_k=question_k,
            search=search,
        )
    else:
        found = retrieve(
            vdb,
            entities,
            evidence=evidence,
            subject=subject or "",
            database=database,
            entity_k=ENTITY_K,
            subject_k=SUBJECT_K,
            search=search,
        )

    attr_ids = [str(hit["id"]) for hit in found.attributes]
    contexts = fetch_attr_column_contexts(attr_ids, database_name=found.database)
    # An attribute deleted since it was embedded has no context, and one in another database
    # has no database name: neither can be queried with the rest.
    attributes = [
        {**hit, **contexts[str(hit["id"])]}
        for hit in found.attributes
        if str(hit["id"]) in contexts and contexts[str(hit["id"])]["database_name"]
    ]

    anchor = next((a for a in attributes if str(a["id"]) == anchor_attribute_id), None) or next(
        iter(attributes), None
    )

    return GroundingPack(
        question=question,
        database=found.database,
        attributes=attributes,
        terms=found.terms,
        term_synonyms=fetch_term_synonyms([str(a["id"]) for a in attributes]),
        tables=_tables(attributes, found.terms, found.database),
        anchor=_place(anchor) if anchor else None,
        join_paths=_join_paths(anchor, attributes) if anchor else [],
        coverage=found.coverage,
    )


def _place(attribute: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": attribute["id"],
        **{f: attribute[f] for f in _PLACE},
        "datatype": attribute.get("datatype") or "",
    }


def _join_paths(anchor: dict[str, Any], attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The path from the anchor's table to each other attribute's, one BFS per table."""
    by_table: dict[str, list[dict[str, str]]] = {}
    paths: list[dict[str, Any]] = []
    for dest in attributes:
        if dest["id"] == anchor["id"]:
            continue
        table_id = str(dest["table_id"])
        if table_id not in by_table:
            by_table[table_id] = find_join_path(anchor["col_id"], dest["col_id"])
        paths.append({**_place(dest), "path": by_table[table_id]})
        logger.debug(
            "join path to %s.%s: %d hop(s)",
            dest["table_name"],
            dest["col_name"],
            len(by_table[table_id]),
        )
    return paths


def _tables(
    attributes: list[dict[str, Any]], terms: list[Hit], database: str | None
) -> list[dict[str, Any]]:
    table_ids = [str(a["table_id"]) for a in attributes]
    if terms:
        pairs = fetch_term_table_pairs(
            term_ids=[str(t["id"]) for t in terms], database_name=database
        )
        table_ids += [str(p["table_id"]) for p in pairs if p.get("table_id")]
    order = list(dict.fromkeys(table_ids))
    tables = {
        str(t["id"]): t
        for t in fetch_tables_by_ids(order)
        if database is None or t.get("database_name") == database
    }
    return [tables[i] for i in order if i in tables]
