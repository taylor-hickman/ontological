# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/term_extractor.py at
# 2a24f5d. The prompt is upstream's, sent through ClaudeGateway.structured() on the `table_terms`
# route. Where upstream's prompt stops at 40 candidate columns (ARCHITECTURE.md §1.3), a wider
# table is asked in chunks of 40, each seeing the Terms the chunks before it proposed. Small
# tables of a pack share one call (`compile.packed`). Synonym extraction, upstream's second call
# here, is the `table_synonyms` stage (`compile.synonyms`).

"""The `table_terms` stage: business Terms for one table, with every candidate column assigned a
display name (ARCHITECTURE.md §1.3 stage 5, §6.3). It sees the descriptions the
`column_descriptions` stage wrote."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ontological.compile import packed
from ontological.compile.descriptions import to_term_name
from ontological.compile.gateway import Structured, ask
from ontological.compile.models import (
    ColumnAttributeSpec,
    PackedTableTerms,
    RawTableTermsResult,
    RawTermProposal,
    TableTermsEntry,
    TermAttributeAssignment,
    TermProposal,
)

TASK = "table_terms"
PROMPT_VERSION = "2a24f5d"
# Upstream's prompt shows a table's first 40 candidate columns and drops the rest (ARCHITECTURE.md
# §1.3). A wider table is asked in chunks of that many instead.
CHUNK_COLUMNS = 40

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
    """One Term named after the table, holding every column: what an invalid answer gives."""
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


def build_prompt(
    table: dict[str, Any],
    specs: list[ColumnAttributeSpec],
    proposed: Sequence[RawTermProposal] = (),
) -> str:
    """Upstream's prompt. A later chunk of a wide table also lists the Terms its earlier chunks
    proposed, so its columns join them rather than start new ones."""
    # Upstream adds a domain summary block when `.semantic_summaries/<db>.json` exists, a file
    # nothing in the engine writes, so it is always empty here.
    spec_lines = "\n".join(_format_spec_line(spec) for spec in specs)
    prompt = (
        f"Table: {table['name']}\n"
        f"Description: {table.get('description') or ''}\n"
        f"Candidate columns (assign each to exactly one Term with a display_name):\n"
        f"{spec_lines}\n"
    )
    if proposed:
        term_lines = "\n".join(
            f"  - {t.name}" + (f" — {t.description}" if t.description else "") for t in proposed
        )
        prompt += (
            f"Terms already proposed for this table's other columns (reuse a name exactly when "
            f"a column belongs to that Term):\n{term_lines}\n"
        )
    return prompt


async def extract_terms(
    gateway: Structured, table: dict[str, Any], specs: list[ColumnAttributeSpec]
) -> list[TermProposal]:
    """One or more Terms for the table, every spec assigned to one; the fallback Term when the
    answer is invalid. A table with more than `CHUNK_COLUMNS` candidate columns is asked in
    chunks of that many, one after another, each seeing the Terms proposed before it; a chunk
    with an invalid answer leaves its columns to the first Term. Raises `ClaudeUnavailable`
    when Claude doesn't answer."""
    if not specs:
        return fallback_terms(table, specs)
    if len(specs) <= CHUNK_COLUMNS:
        result = await ask(
            gateway, TASK, SYSTEM, build_prompt(table, specs), RawTableTermsResult, PROMPT_VERSION
        )
        if result is None:
            return fallback_terms(table, specs)
        return sanitize(result, table=table, specs=specs)
    raw: list[RawTermProposal] = []
    for start in range(0, len(specs), CHUNK_COLUMNS):
        chunk = specs[start : start + CHUNK_COLUMNS]
        result = await ask(
            gateway,
            TASK,
            SYSTEM,
            build_prompt(table, chunk, raw),
            RawTableTermsResult,
            PROMPT_VERSION,
        )
        if result is not None:
            raw = _merge(raw + result.terms, default=to_term_name(table["name"]))
    if not raw:
        return fallback_terms(table, specs)
    return sanitize(RawTableTermsResult(terms=raw), table=table, specs=specs)


def _merge(terms: list[RawTermProposal], *, default: str) -> list[RawTermProposal]:
    """Chunks' Terms with one name as one Term: the first description that says something, and
    every chunk's assignments."""
    merged: dict[str, RawTermProposal] = {}
    for term in terms:
        name = _normalize_label(term.name, fallback=default)
        if (kept := merged.get(name)) is None:
            merged[name] = RawTermProposal(
                name=name, description=term.description, attributes=list(term.attributes)
            )
            continue
        kept.attributes.extend(term.attributes)
        if not kept.description.strip():
            kept.description = term.description
    return list(merged.values())


async def extract_terms_pack(
    gateway: Structured, requests: Sequence[tuple[dict[str, Any], list[ColumnAttributeSpec]]]
) -> list[list[TermProposal]]:
    """`extract_terms` for each `(table, specs)` of a pack, in one call for the tables with
    candidate columns (`compile.packed`). A table the answer leaves out, or one too wide for a
    single prompt, is asked alone."""
    asked = [i for i, (_, specs) in enumerate(requests) if 0 < len(specs) <= CHUNK_COLUMNS]
    found: dict[str, TableTermsEntry] = {}
    if len(asked) > 1:
        sections = [
            (str(table["name"]), build_prompt(table, specs))
            for table, specs in (requests[i] for i in asked)
        ]
        found = await packed.ask_pack(
            gateway, TASK, SYSTEM, sections, PackedTableTerms, lambda r: r.tables
        )
    out: list[list[TermProposal]] = []
    for table, specs in requests:
        entry = found.get(str(table["name"]))
        if entry is None:
            out.append(await extract_terms(gateway, table, specs))
        else:
            out.append(sanitize(entry, table=table, specs=specs))
    return out
