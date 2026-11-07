# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/semantic/bridge_tables.py at
# 2a24f5d. The prompt is upstream's, sent through ClaudeGateway.structured() on the `bridge_sql`
# route (Opus). Upstream picked the owning Term by reranking the candidates against the answer's
# description; rerank is dropped (ARCHITECTURE.md §7.5), so when a bridge has more than one
# candidate the same call lists them and Claude picks one.

"""The bridge stage: a join-pattern SQL attribute for each pure-FK bridge table (ARCHITECTURE.md
§1.3 post-stages, §6.3, §6.4).

A bridge is a table whose every column is a foreign key, declared or semantic, with at least
two, none carrying an attribute, and no bridge attribute yet (the vendored DAL's
`fetch_bridge_table_candidates`). Each is marked a junction, whatever happens next. Its FK
targets' Terms are the candidates to own the attribute; a bridge with none is skipped. One
`bridge_sql` call writes the join SQL, and, given several candidates, names the owner, which
must be one of them; otherwise the first candidate owns it, as upstream's fallback did. The SQL
is written like any SQL attribute (`compile.sql_attribute_writes`), with source `bridgeTable`.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from auto_ontology.dal.datasources import fetch_bridge_table_candidates, mark_table_as_junction
from auto_ontology.dal.terms import get_term_record_for_table
from ontological.catalog.query_history import read_catalog_index
from ontological.compile.gateway import Structured, ask
from ontological.compile.models import (
    BridgeSqlAttributeProposal,
    OwnedBridgeSqlAttributeProposal,
)
from ontological.compile.sql_attribute_writes import create_sql_attribute

logger = logging.getLogger(__name__)

TASK = "bridge_sql"
PROMPT_VERSION = "2a24f5d"
# The prompt with the owner candidates is Ontological's, so it has a version of its own.
OWNER_PROMPT_VERSION = "2a24f5d+owner.1"

SYSTEM = (
    "You are an expert data engineer defining reusable join patterns "
    "for many-to-many junction (bridge) tables. Bridge tables connect "
    "entities through foreign keys, including self-referential cases "
    "where multiple columns point at the same target table."
)


@dataclass(frozen=True)
class Bridges:
    candidates: int  # bridge tables found, each marked a junction
    calls: int
    created: list[str] = field(default_factory=list[str])  # attribute names written
    skipped: int = 0  # no candidate Term, an invalid answer, a conflict, or unresolved SQL


def _format_fk_block(fk_pairs: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for pair in fk_pairs:
        src = pair.get("source_column", "")
        tgt_schema = pair.get("target_schema", "")
        tgt_table = pair.get("target_table", "")
        tgt_col = pair.get("target_column", "")
        lines.append(
            f"  - {src} -> {tgt_schema}.{tgt_table}.{tgt_col}"
            if tgt_schema
            else f"  - {src} -> {tgt_table}.{tgt_col}"
        )
    return "\n".join(lines)


def target_terms(candidate: dict[str, Any]) -> list[dict[str, str]]:
    """Upstream's `_target_terms_for_bridge`: the Terms representing the bridge's distinct FK
    target tables, in FK order, each once."""
    terms: list[dict[str, str]] = []
    seen_terms: set[str] = set()
    seen_tables: set[str] = set()
    for pair in candidate.get("fk_pairs") or []:
        table_id = pair.get("target_table_id")
        if not table_id or table_id in seen_tables:
            continue
        seen_tables.add(table_id)
        record = get_term_record_for_table(table_id)
        if not record or not record.get("id"):
            logger.warning(
                "Bridge FK target table %r (%s) has no REPRESENTS Term.",
                pair.get("target_table"),
                table_id,
            )
            continue
        if record["id"] in seen_terms:
            continue
        seen_terms.add(record["id"])
        terms.append(record)
    return terms


def build_prompt(candidate: dict[str, Any]) -> str:
    """Upstream's prompt for one bridge."""
    schema_name = candidate.get("schema_name") or ""
    table_name = candidate.get("table_name") or ""
    fk_pairs = candidate.get("fk_pairs") or []
    qualified_bridge = f"{schema_name}.{table_name}" if schema_name else table_name

    related_tables = sorted(
        {
            (
                f"{pair['target_schema']}.{pair['target_table']}"
                if pair.get("target_schema")
                else pair["target_table"]
            )
            for pair in fk_pairs
            if pair.get("target_table")
        }
    )
    self_referential = len(related_tables) == 1
    join_hint = (
        "This is a self-referential bridge: both FK columns point at the same "
        "entity table. Alias that table twice in the JOIN (e.g. product AS buyer "
        "and product AS also_bought) so each FK role is clear."
        if self_referential
        else "Join the bridge table to each related entity table using the FK pairs."
    )
    return (
        f"Bridge table: {qualified_bridge}\n"
        f"Related tables: {', '.join(related_tables) or '(unknown)'}\n"
        f"Foreign key columns (use these exact join keys):\n"
        f"{_format_fk_block(fk_pairs)}\n\n"
        f"{join_hint}\n\n"
        "Produce one SqlAttribute that captures how to join through this "
        "bridge table to reach all related entities.\n\n"
        "Rules:\n"
        "  • expression must be a valid SELECT … FROM … JOIN … statement\n"
        "  • schema-qualify every table name\n"
        "  • use ONLY the FK pairs listed above — do not invent columns "
        "or filters\n"
        "  • no WHERE clause with literal business filters; structural "
        "joins only\n"
        "  • name — user-friendly Title Case label (e.g. 'Order Products', "
        "'Also Buy')\n"
        "  • description — what relationship the junction represents\n"
    )


def build_owner_prompt(candidate: dict[str, Any], terms: Sequence[dict[str, str]]) -> str:
    """Upstream's prompt, then the candidate owning Terms to pick from."""
    listed = "\n".join(
        f"  - {t.get('name', '')}"
        + (f": {desc}" if (desc := (t.get("description") or "").strip()) else "")
        for t in terms
    )
    return (
        build_prompt(candidate)
        + "\nCandidate owning terms (the business terms the related tables represent):\n"
        + listed
        + "\n\n"
        "  • owner_term — the one candidate term this SqlAttribute belongs to: the entity a "
        "data consumer would start from when using this relationship. Copy its name exactly "
        "as listed.\n"
    )


def _owner(terms: Sequence[dict[str, str]], named: str) -> dict[str, str]:
    """The candidate `named` names, ignoring case and surrounding space; else the first, as
    upstream's fallback was."""
    folded = named.strip().casefold()
    return next((t for t in terms if (t.get("name") or "").strip().casefold() == folded), terms[0])


async def propose(
    gateway: Structured, candidate: dict[str, Any], terms: Sequence[dict[str, str]]
) -> tuple[BridgeSqlAttributeProposal, dict[str, str]] | None:
    """The bridge's attribute and its owning Term; None for an invalid answer. Raises
    `ClaudeUnavailable` when Claude doesn't answer."""
    if len(terms) == 1:
        one = await ask(
            gateway, TASK, SYSTEM, build_prompt(candidate), BridgeSqlAttributeProposal,
            PROMPT_VERSION,
        )  # fmt: skip
        return None if one is None else (one, terms[0])
    owned = await ask(
        gateway,
        TASK,
        SYSTEM,
        build_owner_prompt(candidate, terms),
        OwnedBridgeSqlAttributeProposal,
        OWNER_PROMPT_VERSION,
    )
    return None if owned is None else (owned, _owner(terms, owned.owner_term))


def _candidates(database_name: str) -> list[tuple[dict[str, Any], list[dict[str, str]]]]:
    """Each bridge with its candidate Terms, every bridge marked a junction first: structure is
    evidence enough, and a later failure mustn't lose it (upstream's order)."""
    found = fetch_bridge_table_candidates(database_name)
    for candidate in found:
        mark_table_as_junction(candidate["table_id"])
    return [(c, target_terms(c)) for c in found]


def _write(proposal: BridgeSqlAttributeProposal, owner: dict[str, str], database_name: str) -> str:
    return create_sql_attribute(
        name=proposal.name,
        description=proposal.description,
        expression=proposal.expression,
        term_id=owner["id"],
        source="bridgeTable",
        catalog=read_catalog_index(database_name),
    )


async def build_bridge_sql_attributes(database_name: str, gateway: Structured) -> Bridges:
    """A join-pattern SQL attribute for every bridge table of `database_name`, one call a bridge.
    Each is written once answered, so a stop keeps it and a rerun no longer finds the bridge.
    Raises `ClaudeUnavailable` when Claude doesn't answer."""
    found = await asyncio.to_thread(_candidates, database_name)
    calls = skipped = 0
    created: list[str] = []
    for candidate, terms in found:
        table = candidate.get("table_name") or candidate.get("table_id")
        if not terms:
            logger.warning("Bridge table %r: no REPRESENTS Terms on FK targets; skipping", table)
            skipped += 1
            continue
        calls += 1
        answer = await propose(gateway, candidate, terms)
        if answer is None:
            logger.warning("Bridge table %r: no valid proposal; skipping", table)
            skipped += 1
            continue
        proposal, owner = answer
        outcome = await asyncio.to_thread(_write, proposal, owner, database_name)
        if outcome == "created":
            logger.info("Bridge table %r: %r on Term %r", table, proposal.name, owner.get("name"))
            created.append(proposal.name)
        else:
            logger.info("Bridge table %r: %r not written: %s", table, proposal.name, outcome)
            skipped += 1
    logger.info(
        "%s: %d bridge table(s), %d call(s), %d SQL attribute(s) written",
        database_name,
        len(found),
        calls,
        len(created),
    )
    return Bridges(len(found), calls, created, skipped)
