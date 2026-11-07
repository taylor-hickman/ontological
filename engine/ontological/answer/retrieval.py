# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's
# auto_ontology/retrieval/text_to_sql/agents/candidates_retrieval.py and
# auto_ontology/retrieval/data_access/semantic_search.py at 2a24f5d. Dense search runs through the
# local embedder (ontological/embed/search.py), one search after another. Upstream's LLM intent
# filters are dropped: retrieval runs before the agent, with no Claude call, and the agent judges
# the candidates itself (ARCHITECTURE.md §8.1). SQL attributes (M3.5) and custom analyses (M5.3)
# join the search once something writes them.

"""Dense retrieval over the semantic layer's Terms and column attributes (ARCHITECTURE.md §7,
§8.1).

Each entity searches the attributes, the evidence searches them once more, and the subject
searches the Terms. With no database given, the one that covers the most entities wins, as
upstream's `_select_candidate_database` picks it, and entities it doesn't cover search it again.
The subject searches that database only, where upstream searches every database and drops the
other databases' tables later.
Hits keep upstream's `{text, id, label, score, ...}` shape, `score` being cosine distance (lower
is nearer). The search is dense unless the caller passes another (`Search`): the retrieval
benchmark (§7.4) runs the pack on each of hybrid search's rankings, and M4.3 puts hybrid search
(`ontological.embed.hybrid`) under it for good.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from auto_ontology.semantic.constants import LABEL_COLUMN_ATTRIBUTE, LABEL_TERM
from auto_ontology.vdb.postgres import PostgresVDB
from ontological.embed.search import dense_search

logger = logging.getLogger(__name__)

# Upstream's k values, tuned to its embedder: M3.9 and M4.7 re-derive them for this one (§7.3).
ENTITY_K = 2  # attributes per extracted entity
EVIDENCE_K = 3  # attributes for the evidence text
SUBJECT_K = 1  # Terms for the subject

# The metadata fields a hit carries over (upstream's `_hits_to_semantic_rows`).
_HIT_FIELDS = ("name", "schema_name", "database_name", "data_type", "source")

Hit = dict[str, Any]


class Search(Protocol):
    """`dense_search`'s shape: the `k` best rows for `query` that `where` keeps, best first, as
    upstream's `{text, metadata, _distance}`."""

    def __call__(
        self, vdb: PostgresVDB, query: str, *, k: int = 10, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...


def search_by_label(
    vdb: PostgresVDB,
    text: str,
    label: str,
    k: int,
    database_name: str | None = None,
    search: Search = dense_search,
) -> list[Hit]:
    """The `k` best rows of one label, best first, as upstream's candidate dicts."""
    where: dict[str, Any] = {"label": label}
    if database_name:
        where["database_name"] = database_name
    hits: list[Hit] = []
    for raw in search(vdb, text, k=k, where=where):
        metadata: dict[str, Any] = raw.get("metadata") or {}
        if metadata.get("id") is None:
            continue
        hit: Hit = {
            "text": (raw.get("text") or "").strip(),
            "id": metadata["id"],
            "label": metadata.get("label"),
            "score": float(raw["_distance"]),
        }
        hit.update({f: metadata[f] for f in _HIT_FIELDS if metadata.get(f) is not None})
        hits.append(hit)
    return hits


def _score(hit: Hit) -> float:
    score = hit.get("score")
    return float(score) if score is not None else float("inf")


def _database_name(hit: Hit) -> str | None:
    return str(hit.get("database_name") or "").strip() or None


def dedupe_best_score(hits: Sequence[Hit]) -> list[Hit]:
    """One hit per id, the nearest, nearest first. A hit names every entity that retrieved it
    (`query_entities`), and the evidence if that did (`query_evidence`)."""
    best: dict[str, Hit] = {}
    entities_by_id: dict[str, set[str]] = {}
    evidence_by_id: dict[str, str] = {}
    for hit in hits:
        if hit.get("id") is None:
            continue
        key = str(hit["id"])
        if entity := hit.get("query_entity"):
            entities_by_id.setdefault(key, set()).add(str(entity))
        if evidence := hit.get("query_evidence"):
            evidence_by_id[key] = str(evidence)
        if key not in best or _score(hit) < _score(best[key]):
            best[key] = hit
    result: list[Hit] = []
    for key, hit in best.items():
        out = dict(hit)
        if key in entities_by_id:
            out["query_entities"] = sorted(entities_by_id[key])
        if key in evidence_by_id:
            out["query_evidence"] = evidence_by_id[key]
        result.append(out)
    return sorted(result, key=_score)


@dataclass(frozen=True)
class Coverage:
    """How well one database answers the entities: what upstream ranks databases by."""

    database: str
    entities: frozenset[str]
    hits: int
    score_total: float

    def rank(self) -> tuple[int, int, float, str]:
        return (-len(self.entities), -self.hits, self.score_total, self.database)


def coverage(hits: Sequence[Hit]) -> list[Coverage]:
    """Each database the attribute hits come from, best first: the most entities covered, then
    the most hits, then the least total distance, then the name."""
    entities: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    totals: dict[str, float] = {}
    for hit in hits:
        database = _database_name(hit)
        if database is None:
            continue
        found = entities.setdefault(database, set())
        if entity := str(hit.get("query_entity") or "").strip():
            found.add(entity)
        counts[database] = counts.get(database, 0) + 1
        totals[database] = totals.get(database, 0.0) + _score(hit)
    ranked = [Coverage(d, frozenset(entities[d]), counts[d], totals[d]) for d in entities]
    return sorted(ranked, key=Coverage.rank)


@dataclass(frozen=True)
class Retrieved:
    database: str | None  # the one given, or the one chosen by coverage
    attributes: list[Hit]  # deduplicated, nearest first
    terms: list[Hit]  # the subject's, nearest first
    coverage: list[Coverage] = field(default_factory=list[Coverage])  # when it chose


def _tagged(hits: list[Hit], **tag: str) -> list[Hit]:
    return [{**hit, **tag} for hit in hits]


def retrieve(
    vdb: PostgresVDB,
    entities: Sequence[str],
    *,
    evidence: str = "",
    subject: str = "",
    database: str | None = None,
    entity_k: int = ENTITY_K,
    evidence_k: int = EVIDENCE_K,
    subject_k: int = SUBJECT_K,
    search: Search = dense_search,
) -> Retrieved:
    """Search the attributes per entity and for the evidence, and the Terms for the subject,
    within `database` when it is given. Without one, keep the database that covers the most
    entities and drop the rest, as upstream does; hits with no database are dropped too.

    Only each entity's nearest `ENTITY_K` hits rank the databases, as many as upstream's
    per-entity search returns. Past that, an `entity_k` wider than upstream's (the raw question's)
    would let the database with the most middling hits beat the one with the nearest."""
    entities = list(dict.fromkeys(e.strip() for e in entities if e.strip()))
    evidence, subject = evidence.strip(), subject.strip()

    attributes: list[Hit] = []
    voters: list[Hit] = []
    for entity in entities:
        found = search_by_label(vdb, entity, LABEL_COLUMN_ATTRIBUTE, entity_k, database, search)
        attributes += _tagged(found, query_entity=entity)
        voters += _tagged(found[:ENTITY_K], query_entity=entity)
    if evidence:
        found = search_by_label(vdb, evidence, LABEL_COLUMN_ATTRIBUTE, evidence_k, database, search)
        attributes += _tagged(found, query_evidence=evidence)
        voters += found

    ranking: list[Coverage] = []
    if database is None:
        ranking = coverage(voters)
        if not ranking:
            logger.warning("no hit names a database; dropping %d hit(s)", len(attributes))
            return Retrieved(None, [], [], ranking)
        database = ranking[0].database
        attributes = [h for h in attributes if _database_name(h) == database]
        covered = {str(h.get("query_entity") or "").strip() for h in attributes}
        for entity in (e for e in entities if e not in covered):
            found = search_by_label(vdb, entity, LABEL_COLUMN_ATTRIBUTE, entity_k, database, search)
            attributes += _tagged(found, query_entity=entity)
        logger.info(
            "chose database %r: %d of %d entities covered",
            database,
            len(ranking[0].entities),
            len(entities),
        )

    terms = (
        search_by_label(vdb, subject, LABEL_TERM, subject_k, database, search) if subject else []
    )
    return Retrieved(database, dedupe_best_score(attributes), terms, ranking)
