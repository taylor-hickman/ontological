# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/term_extractor.py
# (`_extract_synonyms`) at 2a24f5d. The prompt is upstream's, sent through
# ClaudeGateway.structured() on the `table_synonyms` route. Upstream asks only when the terms call
# answered; here a table with a description is asked whichever way the terms call went, since the
# synonyms come from the description alone. Small tables of a pack share one call.

"""The `table_synonyms` stage: alternate names written in a table's description, for its Terms
(ARCHITECTURE.md §1.3 stage 6, §6.3). A table with no description isn't asked."""

from __future__ import annotations

from collections.abc import Sequence

from ontological.compile import packed
from ontological.compile.gateway import Structured, ask
from ontological.compile.models import PackedSynonyms, SynonymExtraction, TableSynonymsEntry

TASK = "table_synonyms"
PROMPT_VERSION = "2a24f5d"

SYSTEM = """\
Your only task is to extract alternate names for a business entity from a table description.

Look for abbreviations, acronyms, or aliases that are explicitly written — for example \
through phrases like "also referred to as", "also known as", "abbreviated as", \
"short for", "stands for", or a parenthetical directly after the entity name such as \
"business unit (BU)".

Return each alternate name verbatim as a separate entry in synonyms.
Example: "business unit (BU)" → synonyms: ["BU"]

Return an empty list if no alternate names are explicitly stated. \
Do NOT infer, guess, or add any name that is not directly written in the description."""


def build_prompt(description: str) -> str:
    return f"Description: {description}"


def _clean(synonyms: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in synonyms if s.strip()))


async def extract_synonyms(gateway: Structured, description: str | None) -> list[str]:
    """The alternate names `description` writes out; none when there is no description or the
    answer is invalid. Raises `ClaudeUnavailable` when Claude doesn't answer."""
    if not description:
        return []
    result = await ask(
        gateway, TASK, SYSTEM, build_prompt(description), SynonymExtraction, PROMPT_VERSION
    )
    return [] if result is None else _clean(result.synonyms)


async def extract_synonyms_pack(
    gateway: Structured, tables: Sequence[tuple[str, str | None]]
) -> list[list[str]]:
    """`extract_synonyms` for each `(name, description)` of a pack, in one call for the tables
    with a description (`compile.packed`). A table the answer leaves out is asked alone."""
    asked = [i for i, (_, description) in enumerate(tables) if description]
    found: dict[str, TableSynonymsEntry] = {}
    if len(asked) > 1:
        sections = [
            (name, f"Table: {name}\n{build_prompt(description or '')}")
            for name, description in (tables[i] for i in asked)
        ]
        found = await packed.ask_pack(
            gateway, TASK, SYSTEM, sections, PackedSynonyms, lambda r: r.tables
        )
    out: list[list[str]] = []
    for name, description in tables:
        entry = found.get(name)
        if entry is None:
            out.append(await extract_synonyms(gateway, description))
        else:
            out.append(_clean(entry.synonyms))
    return out
