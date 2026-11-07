"""The verifier: BIRD's set rule, the failure codes, and the dialect-artifact flag."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from ontological_eval.verify import result_sets_match, score


def test_bird_s_rule_ignores_row_order_and_duplicate_rows() -> None:
    assert result_sets_match([(1, "a"), (2, "b")], [(2, "b"), (1, "a")])
    assert result_sets_match([(1,)], [(1,), (1,), (1,)])
    assert not result_sets_match([(1, "a")], [(1, "a", None)])
    assert not result_sets_match([(1,)], [])


def test_bird_s_rule_compares_values_as_python_does() -> None:
    # 1 == 1.0 == True in a set, as in BIRD's own evaluator.
    assert result_sets_match([(1,)], [(1.0,)])
    assert result_sets_match([(1,)], [(True,)])
    assert not result_sets_match([("1",)], [(1,)])


def test_unhashable_cells_fall_back_to_a_sorted_repr_compare() -> None:
    assert result_sets_match([([1, 2],), ([3],)], [([3],), ([1, 2],)])
    assert not result_sets_match([([1, 2],)], [([2, 1],)])


def test_a_match_scores_one() -> None:
    s = score([(1,), (2,)], [(2,), (1,)])
    assert s.match and s.failure is None
    assert (s.gold_rows, s.pred_rows) == (2, 2)
    assert s.gold_sample == []


def test_a_wrong_result_keeps_a_sample_of_both_sides() -> None:
    s = score([(dt.date(2016, 1, 1), Decimal("2.5"))], [(3,)])
    assert not s.match and s.failure == "wrong_result" and not s.dialect_artifact
    assert s.gold_sample == [["2016-01-01", "2.5"]]
    assert s.pred_sample == [[3]]
    json.dumps(s.to_json())


def test_rows_that_agree_after_normalization_are_a_dialect_artifact_and_still_miss() -> None:
    # Postgres's numeric against DuckDB's double, and SQLite's text date against DuckDB's DATE.
    for gold, pred in [
        ([(Decimal("0.1"),)], [(0.1,)]),
        ([(Decimal("33.3333333333"),)], [(33.33333333333333,)]),
        ([("2012-03-01",)], [(dt.date(2012, 3, 1),)]),
        ([("2012-03-01 10:00:00",)], [(dt.datetime(2012, 3, 1, 10),)]),
    ]:
        s = score(gold, pred)
        assert not s.match and s.dialect_artifact, (gold, pred)
    # SQLite's integer division against DuckDB's is a different answer, not an artifact.
    assert not score([(3,)], [(3.5,)]).dialect_artifact


def test_failures_say_why() -> None:
    gold = [(1,)]
    assert score(gold, None, no_answer=True).failure == "no_answer"
    assert score(gold, None).failure == "no_sql"
    failed = score(gold, RuntimeError("Binder Error: no column x\nLINE 1"))
    assert failed.failure == "pred_error" and failed.error == "Binder Error: no column x"
    assert score(gold, [(1,)], pred_truncated=True).failure == "pred_truncated"
    gold_failed = score(RuntimeError("timeout"), [(1,)])
    assert gold_failed.failure == "gold_error" and not gold_failed.match
