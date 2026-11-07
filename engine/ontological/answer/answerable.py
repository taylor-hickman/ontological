# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/retrieval/entity_coverage/ at
# 2a24f5d (prompts.py, models.py, agents/question_extraction.py, agents/coverage.py) and the
# per-entity search of text_to_sql/agents/candidates_retrieval.py. The extraction runs on the
# `answerable_extract` route (Haiku, ARCHITECTURE.md §6.3); its prompt drops the Glossary and
# Evidence sections, which the pre-check has neither of. The threshold is the embedding model's
# `coverage_distance` (§7.3), not upstream's 0.75. Upstream's LLM filter of SQL attributes and
# custom analyses is dropped: it never changes the score, and the pre-check makes one call. An
# entity is covered by its own hits' distances, where upstream lets an attribute two entities
# found cover both at the nearer one's distance.

"""`check_answerable` (ARCHITECTURE.md §8.3): does the semantic layer cover a question?

One `answerable_extract` call rewrites the question and extracts its entities, each entity
searches the column attributes (`ENTITY_K` of them) within the database, and the score is the
fraction of entities with an attribute hit at or under the coverage distance. With no database
given, the one whose attributes cover the most entities is chosen, as `retrieve` chooses it.

If the extraction fails, the question stands in as the one entity, as upstream does, and the
result says so (`extracted=False`). The raw question is what M3.9 calibrated the distance on.
"""

# The vendored DAL is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, Field

from auto_ontology.dal.attributes import fetch_attr_column_contexts
from auto_ontology.semantic.constants import LABEL_COLUMN_ATTRIBUTE
from auto_ontology.vdb.postgres import PostgresVDB
from ontological.answer.retrieval import ENTITY_K, Hit, Search, coverage, search_by_label
from ontological.embed.model import COVERAGE_DISTANCE
from ontological.embed.search import dense_search

logger = logging.getLogger(__name__)

TASK = "answerable_extract"
PROMPT_VERSION = "2a24f5d"  # upstream's entity-coverage extraction, as ported
MAX_ENTITIES = 5  # upstream's CLARIFICATION_MAX_ENTITIES default


class Extraction(BaseModel):
    """Upstream's `QuestionExtractionLiteModel`."""

    sanitized_question: str = Field(
        description=(
            "Concise, SQL-ready rewrite of the user's request. "
            "Preserve factual constraints and remove narrative fluff."
        ),
    )
    required_entity_name: list[str] = Field(
        description=(
            "Concepts present in the completed sanitized question that refer to "
            "database entities. When words describe a single filterable item, keep them in one "
            "phrase instead of splitting. Keep brand names, product names, and "
            "descriptive named constants; omit numeric literals, dates, and "
            "number-based thresholds."
        ),
    )


SYSTEM = f"""You rewrite conversational user requests into concise, SQL-ready questions and \
extract database entity noun phrases from the sanitized intent.

## Part 1 — sanitized_question

Rules:
- Remove personal background, narrative fluff, filler, politeness, and generic request
  framing such as "please", "can you", "show me", "find", or
  "get semantic objects related to". Keep only the underlying domain intent.
- Preserve every factual constraint: numbers, product names, brands, categories, and \
qualifiers such as "similar", "natural ingredients", or "expensive is okay".
- Do NOT invent constraints that are not in the original text.
- Output one concise question or search intent, not a paragraph.

Examples:

Input: We're planning a road trip next summer and my whole family loves hiking.
I need a tent that can fit 4 people, and lighter is better since we'll carry it.
sanitized_question: Find a 4-person tent, prioritizing lighter weight.

Input: How many shipments were delivered last month?
sanitized_question: How many shipments were delivered last month?

## Part 2 — required_entity_name

Populate "required_entity_name" with 1-{MAX_ENTITIES} noun phrases that correspond to database \
tables, columns, or relationships. Extract only from the completed sanitized intent.

Preserve the exact casing of terms as they appear in the completed sanitized question.
Do not lowercase, uppercase, or normalize them.

Guidelines for what to include in required_entity_name:
- Subject nouns and domain terms ("invoice", "customer", "shipment")
- Qualified entity phrases that combine a subject with its relevant action or attribute
  ("order shipment", "employee hire", "ticket resolution")
- Aggregation-qualified metric rule: when "count", "total", "average", "sum", "min",
  or "max" is attached to a domain noun as the name of a requested metric or column,
  keep it as one entity phrase. This does not
  apply when the aggregation is only how the user asks a question, such as
  "How many shipments..."; in that case, extract the subject entity "shipment".
- Filter-item rule: when several words together describe a single item the user wants to
  filter or search for, keep them in one phrase. Do not split modifier, noun, and purpose
  of the same filter item into separate entries.
  Example: "waterproof hiking tent for family camping" → \
["waterproof hiking tent for family camping"],
  not ["waterproof hiking tent", "family camping"].
- Keep names and descriptive text that identify something: brand names, product names,
  vendor names, categories, and other named constants (e.g. "Salomon Speedcross").
- For interrogative words (who/what/which/whose), resolve to the implied entity type
  AND, if the question contains a qualifying descriptor, include it twice: once alone
  and once combined with the resolved type.
  Example: "who are the active assignees" → ["assignee", "active assignee"]

Guidelines for what to exclude from required_entity_name:
- Bare action verbs ("submitted", "approved", "closed", "assigned")
- Numeric values: counts, amounts, prices, years, and other number literals
- Date/time values when they are numeric or calendar literals
- Aggregation indicators ("count", "total", "average", "sum", "min", "max")
  when standing alone; preserve them when the aggregation-qualified metric rule applies
- Status and filter adjectives when standing alone ("open", "active", "high-priority")
- Bare schema-generic words with no domain meaning on their own: "id", "name",
  "type", "code", "key", "value", "description", "label", "title", "flag",
  "uuid", "pk", "fk". Do not emit these as standalone entities.

Date rule: When a question references a time-qualified event, collapse subject + action
+ granularity into one compact phrase ending with "date".
  Example: "invoices closed in Q2" → ["invoice", "invoice closure quarter date"]
  Example: "orders placed last year" → ["order", "order placement date"]

Examples:
  Q: "How many shipments were delivered last month?"
  → required_entity_name: ["shipment", "shipment delivery month date"]

  Q: "Find a waterproof hiking tent for family camping."
  → required_entity_name: ["waterproof hiking tent for family camping"]
"""


def user_prompt(question: str) -> str:
    return f"## Input\n\n{question}\n"


class Structured(Protocol):
    """`ClaudeGateway.structured()`, the one call this makes."""

    async def structured[M: BaseModel](
        self,
        task: str,
        system: str,
        user: str,
        schema: type[M],
        cache_key_extra: str = "",
        *,
        prompt_version: str = "",
    ) -> M | None: ...


class CoveringAttribute(BaseModel):
    id: str
    attribute: str
    term: str
    table: str
    column: str
    distance: float  # cosine distance to the entity


class EntityCoverage(BaseModel):
    entity: str
    covered: bool  # an attribute is at or under the coverage distance
    nearest: float | None  # the nearest attribute's distance; None when the database has none
    attributes: list[CoveringAttribute]  # the covering ones, nearest first


class Answerability(BaseModel):
    question: str
    sanitized_question: str
    extracted: bool  # False when the extraction failed and the question stood in for it
    database: str | None  # the one given, or the one chosen; None when nothing matched
    coverage: float  # the fraction of entities covered, 0 to 1
    max_distance: float  # the coverage distance it was scored at
    entities: list[EntityCoverage]
    missing: list[str]  # the entities nothing covers, in order


Contexts = Callable[[list[str], str | None], dict[str, dict[str, Any]]]


def _store_contexts(ids: list[str], database: str | None) -> dict[str, dict[str, Any]]:
    return fetch_attr_column_contexts(ids, database_name=database)


async def extract(gateway: Structured, question: str) -> tuple[str, list[str], bool]:
    """The sanitized question, its entities, and whether the extraction answered. A failed one
    leaves the question as its own entity; an empty list, the sanitized question."""
    said = await gateway.structured(
        TASK, SYSTEM, user_prompt(question), Extraction, prompt_version=PROMPT_VERSION
    )
    if said is None:
        logger.warning("entity extraction failed; the question stands in for its entities")
        return question, [question], False
    sanitized = said.sanitized_question.strip() or question
    entities = list(dict.fromkeys(e.strip() for e in said.required_entity_name if e.strip()))
    return sanitized, entities or [sanitized], True


def entity_hits(
    vdb: PostgresVDB,
    entities: Sequence[str],
    database: str | None,
    *,
    search: Search = dense_search,
) -> tuple[str | None, dict[str, list[Hit]]]:
    """Each entity's `ENTITY_K` nearest attributes in `database`. Without one, the database that
    covers the most entities (`coverage`), or None when no hit names a database."""
    if database is None:
        voters = [
            {**h, "query_entity": e}
            for e in entities
            for h in search_by_label(vdb, e, LABEL_COLUMN_ATTRIBUTE, ENTITY_K, None, search)
        ]
        ranking = coverage(voters)
        if not ranking:
            return None, {e: [] for e in entities}
        database = ranking[0].database
    return database, {
        e: search_by_label(vdb, e, LABEL_COLUMN_ATTRIBUTE, ENTITY_K, database, search)
        for e in entities
    }


def grade(
    hits: dict[str, list[Hit]],
    contexts: dict[str, dict[str, Any]],
    max_distance: float,
) -> list[EntityCoverage]:
    """Upstream's coverage grade, per entity: covered when one of its own attribute hits is at
    or under `max_distance`. A hit with no store context (deleted since it was embedded, or in
    another database) is skipped."""
    out: list[EntityCoverage] = []
    for entity, found in hits.items():
        placed = sorted(
            (h for h in found if str(h["id"]) in contexts), key=lambda h: float(h["score"])
        )
        covering = [
            CoveringAttribute(
                id=str(h["id"]),
                attribute=str(contexts[str(h["id"])].get("attr_name") or h.get("name") or ""),
                term=str(contexts[str(h["id"])].get("term_name") or ""),
                table=str(contexts[str(h["id"])].get("table_name") or ""),
                column=str(contexts[str(h["id"])].get("col_name") or ""),
                distance=round(float(h["score"]), 4),
            )
            for h in placed
            if float(h["score"]) <= max_distance
        ]
        out.append(
            EntityCoverage(
                entity=entity,
                covered=bool(covering),
                nearest=round(float(placed[0]["score"]), 4) if placed else None,
                attributes=covering,
            )
        )
    return out


async def check_answerable(
    question: str,
    *,
    gateway: Structured,
    vdb: PostgresVDB,
    database: str | None = None,
    max_distance: float = COVERAGE_DISTANCE,
    search: Search = dense_search,
    contexts: Contexts = _store_contexts,
) -> Answerability:
    """How well the semantic layer covers `question`, within `database` when it is given. Makes
    one Claude call (answered from the cache for a question asked before); the searches and the
    store reads run on a worker thread. `max_distance` is the embedding model's
    `coverage_distance`: pass the engine's own."""
    sanitized, entities, extracted = await extract(gateway, question)
    return await asyncio.to_thread(
        score,
        question,
        sanitized,
        entities,
        extracted=extracted,
        vdb=vdb,
        database=database,
        max_distance=max_distance,
        search=search,
        contexts=contexts,
    )


def score(
    question: str,
    sanitized: str,
    entities: Sequence[str],
    *,
    extracted: bool,
    vdb: PostgresVDB,
    database: str | None,
    max_distance: float,
    search: Search = dense_search,
    contexts: Contexts = _store_contexts,
) -> Answerability:
    """The deterministic half of `check_answerable`, on entities already extracted."""
    chosen, hits = entity_hits(vdb, entities, database, search=search)
    ids = list(dict.fromkeys(str(h["id"]) for found in hits.values() for h in found))
    known = contexts(ids, chosen) if ids else {}
    placed = {i: c for i, c in known.items() if c.get("database_name")}
    graded = grade(hits, placed, max_distance)
    covered = sum(e.covered for e in graded)
    logger.info("coverage %d/%d in %r at %.3f", covered, len(graded), chosen, max_distance)
    return Answerability(
        question=question,
        sanitized_question=sanitized,
        extracted=extracted,
        database=chosen,
        coverage=round(covered / len(graded), 4) if graded else 0.0,
        max_distance=max_distance,
        entities=graded,
        missing=[e.entity for e in graded if not e.covered],
    )
