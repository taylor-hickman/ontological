# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: built from upstream's four per-table prompts at 2a24f5d
# (fk_suggester.py, deterministic.py, term_extractor.py), each kept verbatim as a section of one
# `table_semantics` call. Upstream asks them as separate calls.

"""The merged option (`compile.table_calls = "merged"`): one `table_semantics` call per table in
place of the four split stages (ARCHITECTURE.md §6.3, §6.4).

The call's system prompt is the four stages' system prompts as numbered sections, and its user
turn is each stage's own prompt for the table under the same numbers. The answer
(`models.TableSemantics`) goes through each stage's own clean-up, so what a table gets written
has the same shape as a split compile's. One call can't see its own answer, so where split runs
the stages in order, here:

- the descriptions and Terms sections list every column that isn't a declared FK, and a column
  the keys answer suggests as an FK is dropped afterwards, as split leaves it out of both;
- the Terms section shows a column's existing description, and the model names the Terms with
  the descriptions it is writing in the same answer, where split's terms call reads them.

A table merged doesn't suit is left to the split stages: one wider than `terms.CHUNK_COLUMNS`
candidate columns, which split asks in chunks, and one with no candidate columns, for which
split asks only its keys. So is a table whose answer is invalid: it is asked the split stages'
calls rather than taking their fallbacks, so the option never trades quality for calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ontological.compile import descriptions, keys, synonyms, terms
from ontological.compile.gateway import Structured, ask
from ontological.compile.models import (
    ColumnAttributeSpec,
    RawTableTermsResult,
    TableKeys,
    TableSemantics,
    TermProposal,
)

logger = logging.getLogger(__name__)

TASK = "table_semantics"
# The sections are upstream's prompts; their arrangement is Ontological's.
PROMPT_VERSION = "2a24f5d+merged.1"

KEYS = "1. Keys"
DESCRIPTIONS = "2. Column descriptions"
TERMS = "3. Terms"
SYNONYMS = "4. Synonyms"

SYSTEM = f"""\
You document one relational table for a semantic layer. The request has four sections, and \
each has its own instructions below. Answer every section in one response, each in the fields \
its heading names. Each section's instructions apply to that section alone.

## {KEYS} (fk_suggestions, pk_column_names, is_junction_table, junction_table_rationale)

{keys.SYSTEM}

## {DESCRIPTIONS} (column_descriptions)

{descriptions.SYSTEM}

## {TERMS} (terms)

{terms.SYSTEM}

## {SYNONYMS} (synonyms)

{synonyms.SYSTEM}

## How the sections fit together

- A column you list in fk_suggestions references another table: leave it out of terms.
- Name the Terms and display labels in the light of the column descriptions you write.
- A section that says "(nothing to ask)" takes the empty answer: no fk_suggestions, \
pk_column_names or junction, no column_descriptions, or no synonyms."""

NOTHING = "(nothing to ask)"


@dataclass
class Semantics:
    """A table's answer, cleaned up as each split stage cleans up its own."""

    keys: TableKeys
    specs: list[ColumnAttributeSpec]  # the non-FK columns, described
    terms: list[TermProposal] = field(default_factory=list[TermProposal])  # with their synonyms


def build_prompt(
    table: dict[str, Any],
    keys_prompt: str | None,
    undescribed: list[dict[str, Any]],
    specs: list[ColumnAttributeSpec],
    profiling: dict[str, dict[str, Any]],
) -> str:
    """Each stage's own prompt for the table, under its section's heading."""
    description = table.get("description")
    columns = descriptions.build_prompt(undescribed, profiling) if undescribed else NOTHING
    parts = [
        (KEYS, keys_prompt or NOTHING),
        (DESCRIPTIONS, columns),
        (TERMS, terms.build_prompt(table, specs)),
        (SYNONYMS, synonyms.build_prompt(description) if description else NOTHING),
    ]
    return "\n".join(f"## {heading}\n{body.rstrip()}\n" for heading, body in parts)


def suits(ctx: dict[str, Any]) -> bool:
    """Whether merged asks about the table: it has candidate columns (those no declared FK
    starts from), and no more than `terms.CHUNK_COLUMNS`."""
    described, undescribed = descriptions.split_columns(
        ctx.get("columns", []), ctx.get("fks", []), set()
    )
    return 0 < len(described) + len(undescribed) <= terms.CHUNK_COLUMNS


async def table_semantics(
    gateway: Structured,
    table: dict[str, Any],
    ctx: dict[str, Any],
    profiling: dict[str, dict[str, Any]] | None = None,
) -> Semantics | None:
    """The table's keys, described columns and Terms from one call; None when the table is left
    to the split stages: merged doesn't suit it, or the answer is invalid. Raises
    `ClaudeUnavailable` when Claude doesn't answer."""
    profiling = profiling or {}
    columns: list[dict[str, Any]] = ctx.get("columns", [])
    fks: list[dict[str, Any]] = ctx.get("fks", [])
    if not suits(ctx):
        return None
    described, undescribed = descriptions.split_columns(columns, fks, set())
    built = keys.build_prompt(table, ctx, profiling)
    # Before the answer, the Terms section shows only the descriptions the columns already have.
    shown = descriptions.column_specs(described, undescribed, {}, profiling)
    result = await ask(
        gateway,
        TASK,
        SYSTEM,
        build_prompt(table, built[0] if built else None, undescribed, shown, profiling),
        TableSemantics,
        PROMPT_VERSION,
    )
    if result is None:
        logger.warning("[%s] invalid table_semantics answer; asking split", table["name"])
        return None

    found = keys.sanitize(result, built[1], profiling) if built else TableKeys()
    suggested = {s.column_name for s in found.suggestions}
    described, undescribed = descriptions.split_columns(columns, fks, suggested)
    written = descriptions.matched(undescribed, result.column_descriptions)
    specs = descriptions.column_specs(described, undescribed, written, profiling)
    if not specs:
        return Semantics(found, specs)
    proposed = terms.sanitize(RawTableTermsResult(terms=result.terms), table=table, specs=specs)
    words = synonyms.clean(result.synonyms) if table.get("description") else []
    if words:
        for term in proposed:
            term.synonyms = words
    return Semantics(found, specs, proposed)
