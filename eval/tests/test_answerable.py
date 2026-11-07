"""What the entity calibration reports `check_answerable` would return, on distances written by
hand. The extraction and the distances themselves are the engine's (engine/tests/answer/
test_answerable.py) and the retrieval benchmark's (test_retrieval.py)."""

from __future__ import annotations

from ontological_eval.answerable import Extracted, at_threshold, covered


def extracted(id: int, db: str, *entities: str) -> Extracted:
    return Extracted("bird", id, db, "q", "q", list(entities), True)


def test_a_question_is_covered_by_the_fraction_of_entities_within_the_distance() -> None:
    assert covered([0.4, 0.7, None], 0.6) == 1 / 3
    assert covered([0.4, 0.6], 0.6) == 1.0
    assert covered([], 0.6) == 0.0


def test_at_threshold_splits_own_database_from_the_others() -> None:
    questions = [extracted(1, "a", "x", "y"), extracted(2, "b", "z")]
    nearest: dict[str, dict[str, list[float | None]]] = {
        "bird:1": {"a": [0.3, 0.5], "b": [0.55, 0.9]},
        "bird:2": {"a": [0.2], "b": [0.65]},
    }

    got = at_threshold(0.6, questions, nearest)

    assert (got.entity_tpr, got.entity_fpr) == (round(2 / 3, 4), round(2 / 3, 4))
    assert (got.own_mean, got.other_mean) == (0.5, 0.75)  # q1 1.0 and q2 0.0; q1 0.5, q2 1.0
    assert (got.own_full, got.other_full) == (0.5, 0.5)
