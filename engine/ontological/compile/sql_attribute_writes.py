# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: compile's half of upstream's
# auto_ontology/server/sql_attributes/service.py (`create_sql_attribute`) at 2a24f5d. The checks
# are upstream's, in its order. The SQL is resolved against the database's catalog with the
# engine's port of upstream's parser (`catalog.sql_parse`), and the statement is written with
# `catalog.query_history`. Nothing is embedded here: the semantic layer's sync embeds the new
# attributes after the compile (ontological/embed/semantic.py).

"""Writing the SQL attributes compile proposes (ARCHITECTURE.md §1.1, §1.3).

An attribute is a row in `sql_attribute`, its statement in `sql_query` (with the tables and
columns it reads), the link between them (`sql_attribute__sql`) and its Term
(`sql_attribute__term`). A name another attribute holds, or SQL its Term already has, is a
conflict, and SQL that touches no table of the catalog is refused, as upstream's are.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import insert

from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store, write_transaction
from auto_ontology.dal.sql_attributes import (
    find_attr_by_expression,
    find_attr_by_name,
    link_to_term,
)
from ontological.catalog.parsers.catalog_index import CatalogIndex
from ontological.catalog.query_history import write_statement
from ontological.catalog.sql_parse import parse_query

logger = logging.getLogger(__name__)

#: Upstream's dialects for a database with no connector to name one (`server/sql_utils.py`):
#: sqlglot's generic parser, then Postgres. Compile has no connector dialect.
DIALECTS = ("", "postgres")

Source = Literal["sql", "bridgeTable"]
Written = Literal["created", "name_taken", "sql_taken", "unresolved"]


def create_sql_attribute(
    *,
    name: str,
    description: str,
    expression: str,
    term_id: str,
    source: Source,
    catalog: CatalogIndex,
    dialects: Sequence[str] = DIALECTS,
) -> Written:
    """Write a SQL attribute under `term_id`, in one transaction, unless its name is taken, its
    Term already has the same SQL (case and runs of space aside), or the SQL doesn't resolve
    against `catalog`. Says which."""
    if find_attr_by_name(name, exclude_id=None) is not None:
        return "name_taken"
    if find_attr_by_expression(term_id=term_id, expression=expression, exclude_id=None):
        return "sql_taken"
    try:
        parsed = parse_query(expression, catalog, dialects)
    except Exception as e:
        logger.info("SQL attribute %r doesn't parse: %s", name, e)
        return "unresolved"
    if parsed is None:
        logger.info("SQL attribute %r reads no table of the catalog", name)
        return "unresolved"

    attr_id = str(uuid.uuid4())
    with write_transaction():
        store().query_write(
            insert(s.sql_attribute).values(
                id=attr_id,
                name=name,
                description=description,
                expression=expression,
                source=source,
            )
        )
        sql_id = write_statement(parsed)
        store().query_write(
            insert(s.sql_attribute__sql).values(attribute_id=attr_id, sql_query_id=sql_id)
        )
        link_to_term(attr_id, term_id)
    return "created"
