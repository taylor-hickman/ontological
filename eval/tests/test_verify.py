"""The verifier: BIRD's set rule, the failure codes, and the dialect-artifact tolerance."""

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


def test_rows_that_agree_under_the_tolerance_rule_match_and_are_flagged() -> None:
    # The M1.10 review: each artifact the smoke runs flagged was the right answer.
    for gold, pred in [
        # Postgres's numeric against DuckDB's double.
        ([(Decimal("0.1"),)], [(0.1,)]),
        ([(Decimal("33.3333333333"),)], [(33.33333333333333,)]),
        # SQLite's double against DuckDB's float32, which is what `CAST(x AS REAL)` gives (bird:3).
        ([(0.5714285714285714,)], [(0.5714285969734192,)]),
        # SQLite's text dates against DuckDB's DATE and TIMESTAMP (bird:212).
        ([(8489, "1993-09-18")], [(8489, dt.date(1993, 9, 18))]),
        ([("2012-03-01 10:00:00",)], [(dt.datetime(2012, 3, 1, 10),)]),
    ]:
        s = score(gold, pred)
        assert s.match and s.failure is None and s.dialect_artifact, (gold, pred)
        assert s.gold_sample and s.pred_sample  # kept, so the report can list it
    # An exact match isn't an artifact.
    assert not score([(1,)], [(1.0,)]).dialect_artifact


def test_the_tolerance_rule_keeps_whole_numbers_exact() -> None:
    # Counts and sums agree to 6 significant digits without being the same answer.
    for gold, pred in [
        ([(1234567,)], [(1234568,)]),
        ([(1234567,)], [(1234568.0,)]),
        ([(Decimal("207324"),)], [(207325,)]),
    ]:
        s = score(gold, pred)
        assert not s.match and not s.dialect_artifact, (gold, pred)
    # Integral values of different types are already equal under BIRD's rule.
    assert score([(Decimal("207324.00"),)], [(207324,)]).match


def test_a_different_answer_is_still_a_miss() -> None:
    # SQLite's integer division against DuckDB's: a different answer, not an artifact.
    s = score([(3,)], [(3.5,)])
    assert not s.match and not s.dialect_artifact
    assert not score([(0.571428,)], [(0.571438,)]).match
    assert not score([("1993-09-18",)], [(dt.date(1993, 9, 19),)]).match


def test_failures_say_why() -> None:
    gold = [(1,)]
    assert score(gold, None, no_answer=True).failure == "no_answer"
    assert score(gold, None).failure == "no_sql"
    failed = score(gold, RuntimeError("Binder Error: no column x\nLINE 1"))
    assert failed.failure == "pred_error" and failed.error == "Binder Error: no column x"
    assert score(gold, [(1,)], pred_truncated=True).failure == "pred_truncated"
    gold_failed = score(RuntimeError("timeout"), [(1,)])
    assert gold_failed.failure == "gold_error" and not gold_failed.match
