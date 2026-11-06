# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/term_extractor.py at
# 2a24f5d. The prompt is upstream's, sent through ClaudeGateway.structured() on the `table_terms`
# route. Every candidate column reaches the prompt, where upstream's stops at 40 (ARCHITECTURE.md
# §1.3); chunking a table too wide for one prompt is M3.2's. Synonym extraction, upstream's
# second call here, is the `table_synonyms` stage, which M3.2 adds.

"""The `table_terms` stage: business Terms for one table, with every candidate column assigned a
display name (ARCHITECTURE.md §1.3 stage 5, §6.3). It sees the descriptions the
`column_descriptions` stage wrote."""

from __future__ import annotations

import re
from typing import Any

from ontological.compile.descriptions import to_term_name
from ontological.compile.gateway import Structured
from ontological.compile.models import (
    ColumnAttributeSpec,
    RawTableTermsResult,
    TermAttributeAssignment,
    TermProposal,
)

TASK = "table_terms"
PROMPT_VERSION = "2a24f5d"

_INVALID = {"unnamed", "unknown", "none", ""}
_LABEL_RE = re.compile(r"^[A-Z][A-Za-z0-9]*(?: [A-Za-z][A-Za-z0-9]*)*$")

SYSTEM = """\
You propose business Terms for a relational table and assign candidate columns \
to each Term with user-friendly display labels.

Rules:
1. Strongly prefer ONE Term per table. A second Term is warranted ONLY when the \
table is a true junction between two unrelated entities. Never split a single \
entity into separate Terms for its core fields versus its metadata, lifecycle, \
or audit fields — those all belong to the same Term. If in doubt, use one Term.
2. Term names must be user-friendly with spaces between words (e.g. Purchase Order, \
not purchase_orders or PurchaseOrder).
3. Assign EVERY candidate column to exactly one Term. For each assignment return \
source_column exactly as given and a display_name — a user-friendly ColumnAttribute \
label with spaces between words (e.g. Order Date, Total Amount).
4. Do not propose IS_A, PART_OF, or ROLE relationships.
5. If the table name contains a word that indicates it is a variant of another table \
(e.g. ARC, ARCHIVE, HIST, HISTORY, STAGING, DELETED, TEMP), prefix every display_name \
with a qualifying word derived from that suffix \
(e.g. PEOPLE_ARC → "Archived Is Sales Person", ORDERS_HIST → "Historical Total Amount")."""


def _normalize_label(name: str, *, fallback: str) -> str:
    cleaned = name.strip()
    if cleaned.lower() in _INVALID or not _LABEL_RE.match(cleaned):
        return fallback
    return to_term_name(cleaned)


def _format_spec_line(spec: ColumnAttributeSpec) -> str:
    desc = f" — {spec.description}" if spec.description else ""
    return f"  - source_column={spec.source_column} ({spec.datatype}){desc}"


def apply_display_names(terms: list[TermProposal], specs: list[ColumnAttributeSpec]) -> None:
    """Write the display labels back onto the specs, for the attribute writes."""
    by_column = {spec.source_column: spec for spec in specs}
    for term in terms:
        for attr in term.attributes:
            if (spec := by_column.get(attr.source_column)) is not None:
                spec.display_name = attr.display_name
    for spec in specs:
        if not spec.display_name:
            spec.display_name = spec.name


def fallback_terms(table: dict[str, Any], specs: list[ColumnAttributeSpec]) -> list[TermProposal]:
    """One Term named after the table, holding every column: what a failed call gives."""
    terms = [
        TermProposal(
            name=to_term_name(table["name"]),
            description=f"Business entity represented by table {table['name']}",
            attributes=[TermAttributeAssignment(s.source_column, s.name) for s in specs],
        )
    ]
    apply_display_names(terms, specs)
    return terms


def sanitize(
    result: RawTableTermsResult, *, table: dict[str, Any], specs: list[ColumnAttributeSpec]
) -> list[TermProposal]:
    """Keep each column once, under its first Term, with a usable label. Columns the model left
    out go to the first Term."""
    spec_by_column = {spec.source_column: spec for spec in specs}
    default_term = to_term_name(table["name"])
    terms: list[TermProposal] = []
    seen_names: set[str] = set()
    assigned: set[str] = set()

    for raw_term in result.terms:
        term_name = _normalize_label(raw_term.name, fallback=default_term)
        if term_name in seen_names:
            continue
        seen_names.add(term_name)
        attributes: list[TermAttributeAssignment] = []
        for raw_attr in raw_term.attributes:
            column = raw_attr.source_column.strip()
            if not column or column not in spec_by_column or column in assigned:
                continue
            label = _normalize_label(raw_attr.display_name, fallback=spec_by_column[column].name)
            attributes.append(TermAttributeAssignment(column, label))
            assigned.add(column)
        terms.append(TermProposal(term_name, raw_term.description.strip(), [], attributes))

    if not terms:
        return fallback_terms(table, specs)
    for spec in specs:
        if spec.source_column not in assigned:
            terms[0].attributes.append(TermAttributeAssignment(spec.source_column, spec.name))
            assigned.add(spec.source_column)
    apply_display_names(terms, specs)
    return terms


def build_prompt(table: dict[str, Any], specs: list[ColumnAttributeSpec]) -> str:
    # Upstream adds a domain summary block when `.semantic_summaries/<db>.json` exists, a file
    # nothing in the engine writes, so it is always empty here.
    spec_lines = "\n".join(_format_spec_line(spec) for spec in specs)
    return (
        f"Table: {table['name']}\n"
        f"Description: {table.get('description') or ''}\n"
        f"Candidate columns (assign each to exactly one Term with a display_name):\n"
        f"{spec_lines}\n"
    )


async def extract_terms(
    gateway: Structured, table: dict[str, Any], specs: list[ColumnAttributeSpec]
) -> list[TermProposal]:
    """One or more Terms for the table, every spec assigned to one; the fallback Term when the
    call fails."""
    if not specs:
        return fallback_terms(table, specs)
    result = await gateway.structured(
        TASK,
        SYSTEM,
        build_prompt(table, specs),
        RawTableTermsResult,
        prompt_version=PROMPT_VERSION,
    )
    if result is None:
        return fallback_terms(table, specs)
    return sanitize(result, table=table, specs=specs)
