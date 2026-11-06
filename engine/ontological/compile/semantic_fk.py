# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: the declared-FK fast path of upstream's
# auto_ontology/semantic/semantic_fk.py at 2a24f5d, and its join-default selection with the
# deterministic choice only. Inference for columns without a declared FK (vector search, the
# batched `semantic_fk_pick` call, the value-overlap probe and pruning) is M3.4's.

"""Semantic FKs from declared foreign keys (ARCHITECTURE.md §1.1, §1.3 post-stages).

A column that is a declared foreign key carries no attribute of its own: the compile stages left
it out. Here it gets a `column__semantic_fk` edge to the attribute on the column it references,
which is what join paths (`find_join_path`) walk. A declared FK into its own table is a
self-reference, never the entity it points to, and gets no edge.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from auto_ontology.dal.attributes import (
    fetch_semantic_fk_join_groups,
    find_column_attribute_by_column_id,
    find_unlinked_fk_columns,
    merge_semantic_fk,
    set_semantic_fk_join_default,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SemanticFks:
    created: int  # edges written
    self_references: int  # declared FKs into their own table, which get none
    no_attribute: int  # declared FKs whose target column has no attribute
    undeclared: int  # unlinked columns with no declared FK, which M3.4 resolves


def resolve_declared_fks(database_name: str) -> SemanticFks:
    """Write a semantic FK for every unlinked column with a declared FK, then pick each source
    table's join default per target attribute."""
    created = self_refs = missing = undeclared = 0
    for col in find_unlinked_fk_columns(database_name):
        target_column_id: str | None = col.get("fk_target_col_id")
        if not target_column_id:
            undeclared += 1
            continue
        if col.get("fk_target_table_id") == col.get("table_id"):
            self_refs += 1
            logger.debug("%s.%s references its own table", col["table_name"], col["name"])
            continue
        attr_id = find_column_attribute_by_column_id(target_column_id)
        if attr_id and merge_semantic_fk(col["id"], attr_id):
            created += 1
        else:
            missing += 1
            logger.info(
                "%s.%s: the column it references has no attribute", col["table_name"], col["name"]
            )
    select_join_defaults(database_name)
    logger.info(
        "semantic FKs for %s: %d from declared FKs, %d self-references, %d without a target "
        "attribute, %d undeclared left for inference",
        database_name,
        created,
        self_refs,
        missing,
        undeclared,
    )
    return SemanticFks(created, self_refs, missing, undeclared)


def select_join_defaults(database_name: str) -> int:
    """Enable exactly one semantic FK per (source table, target attribute) for join paths.
    Where several columns of a table reference the same attribute, upstream asks a model and
    falls back to the first column by name; this takes the fallback."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in fetch_semantic_fk_join_groups(database_name):
        groups.setdefault((row["source_table_id"], row["target_attribute_id"]), []).append(row)
    for (table_id, attr_id), group in groups.items():
        chosen = min(
            group,
            key=lambda r: (
                str(r.get("source_column_name") or "").casefold(),
                r["source_column_id"],
            ),
        )
        set_semantic_fk_join_default(table_id, attr_id, chosen["source_column_id"])
    return len(groups)
