"""Retrieval's ranking rules, ported from upstream's candidates_retrieval.py (BUILD_PLAN.md M1.6),
on hits written by hand. tests/store/test_grounding.py runs them on real vectors."""

from __future__ import annotations

from ontological.answer.retrieval import Coverage, coverage, dedupe_best_score


def hit(id: str, score: float | None, database: str = "a", **extra: str) -> dict[str, object]:
    return {"id": id, "score": score, "database_name": database, **extra}


def test_dedupe_keeps_the_nearest_hit_and_every_entity_that_found_it() -> None:
    got = dedupe_best_score(
        [
            hit("x", 0.5, query_entity="revenue"),
            hit("y", 0.2, query_entity="revenue"),
            hit("x", 0.3, query_entity="sales"),
            hit("x", 0.9, query_evidence="sales means revenue"),
            hit("z", None, query_entity="sales"),  # no score sorts last
            {"score": 0.1},  # no id is dropped
        ]
    )

    assert [(h["id"], h["score"]) for h in got] == [("y", 0.2), ("x", 0.3), ("z", None)]
    assert got[1]["query_entities"] == ["revenue", "sales"]
    assert got[1]["query_evidence"] == "sales means revenue"
    assert "query_evidence" not in got[0]


def test_databases_rank_by_entities_covered_then_hits_then_distance_then_name() -> None:
    ranked = coverage(
        [
            hit("1", 0.1, "few", query_entity="revenue"),
            hit("2", 0.5, "wide", query_entity="revenue"),
            hit("3", 0.5, "wide", query_entity="region"),
            hit("4", 0.4, "near", query_entity="revenue"),
            hit("5", 0.1, "near", query_entity="revenue"),
            hit("6", 0.3, "far", query_entity="revenue"),
            hit("7", 0.3, "far", query_entity="revenue"),
            hit("8", 0.3, "", query_entity="revenue"),  # no database: no vote
            hit("9", 0.3, "same", query_entity="revenue"),
            hit("10", 0.3, "same", query_entity="revenue"),
        ]
    )

    assert [c.database for c in ranked] == ["wide", "near", "far", "same", "few"]
    assert ranked[0] == Coverage("wide", frozenset({"revenue", "region"}), 2, 1.0)
