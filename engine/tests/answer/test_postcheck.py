"""The post-run check (ARCHITECTURE.md §8.1, BUILD_PLAN.md M1.7): after the session, the tool log
must show plan → validate → run → review for the SQL the answer gives, with matching ids, or the
answer is shown as unverified with the reason. `information` answers need only a plan, with the
critic off no review is needed, and an `answerable=false` answer needs a plan and no SQL."""

from __future__ import annotations

from typing import Any

import pytest

from ontological.answer.models import AnswerModel
from ontological.answer.postcheck import Verdict, post_run_check
from ontological.llm.gateway import ToolCall

SQL = "SELECT COUNT(*) AS n FROM Album"


def call(name: str, *, failed: bool = False, **data: Any) -> ToolCall:
    return ToolCall(name=name, args={}, is_error=failed, text="", data=data)


def plan(plan_id: str = "plan-1", intent: str = "calculation") -> ToolCall:
    return call("plan", plan_id=plan_id, intent=intent, database="chinook")


def validated(vid: str = "val-1", sql: str = SQL, plan_id: str = "plan-1") -> ToolCall:
    return call("validate_sql", ok=True, validation_id=vid, sql=sql, plan_id=plan_id)


def ran(vid: str = "val-1") -> ToolCall:
    return call("run_sql", validation_id=vid, columns=["n"], rows=[[347]], row_count=1)


def reviewed(rid: str = "rev-1", vid: str = "val-1", passed: bool = True) -> ToolCall:
    return call("review_answer", review_id=rid, validation_id=vid, passed=passed, issues=[])


FULL = [plan(), validated(), ran(), reviewed()]


def answer(**overrides: Any) -> AnswerModel:
    fields: dict[str, Any] = {
        "answer": "There are 347 albums.",
        "sql": SQL,
        "plan_id": "plan-1",
        "validation_id": "val-1",
        "review_id": "rev-1",
        "answerable": True,
    }
    return AnswerModel.model_validate({**fields, **overrides})


def check(log: list[ToolCall], ans: AnswerModel | None, *, critic: bool = True) -> Verdict:
    return post_run_check(log, ans, critic=critic)


def unverified(verdict: Verdict, *needles: str) -> None:
    assert not verdict.verified
    assert verdict.reason is not None
    for needle in needles:
        assert needle in verdict.reason, verdict.reason


# --- calculation: the full sequence ----------------------------------------------------------


def test_the_full_sequence_for_the_answers_sql_is_verified() -> None:
    assert check(FULL, answer()) == Verdict(verified=True, reason=None)


def test_the_sql_is_compared_without_surrounding_space_or_a_trailing_semicolon() -> None:
    assert check(FULL, answer(sql=f"  {SQL};\n")).verified


def test_repairs_along_the_way_are_fine_when_the_last_sql_went_through_every_step() -> None:
    log = [
        plan(),
        call("validate_sql", failed=True, ok=False, issues=["unknown column"]),
        validated("val-1", "SELECT Titel FROM Album"),
        call("run_sql", failed=True, ok=False, error="Binder Error"),
        validated("val-2"),
        ran("val-2"),
        reviewed("rev-1", "val-2", passed=False),
        validated("val-3"),
        ran("val-3"),
        reviewed("rev-2", "val-3"),
    ]
    assert check(log, answer(validation_id="val-3", review_id="rev-2")).verified


def test_no_answer_is_unverified() -> None:
    unverified(check(FULL, None), "no answer")


def test_a_plan_id_plan_did_not_issue_is_unverified() -> None:
    unverified(check(FULL, answer(plan_id="plan-9")), "plan-9")


def test_no_plan_is_unverified() -> None:
    unverified(check(FULL[1:], answer()), "plan")


def test_an_answer_without_sql_is_unverified() -> None:
    unverified(check(FULL, answer(sql=None)), "SQL")


def test_a_validation_id_validate_sql_did_not_issue_is_unverified() -> None:
    unverified(check(FULL, answer(validation_id="val-7")), "val-7")


def test_sql_other_than_the_validated_sql_is_unverified() -> None:
    unverified(check(FULL, answer(sql="SELECT 1 FROM Album")), "isn't the SQL", "val-1")


def test_a_validation_under_another_plan_is_unverified() -> None:
    log = [plan("plan-1"), plan("plan-2"), validated(plan_id="plan-2"), ran(), reviewed()]
    unverified(check(log, answer(plan_id="plan-1")), "plan-2")


def test_sql_that_never_ran_is_unverified() -> None:
    unverified(check([plan(), validated(), reviewed()], answer()), "run_sql")


def test_a_failed_run_is_not_a_run() -> None:
    log = [plan(), validated(), call("run_sql", failed=True, validation_id="val-1"), reviewed()]
    unverified(check(log, answer()), "run_sql")


def test_a_missing_review_is_unverified_with_the_critic_on() -> None:
    unverified(check(FULL[:3], answer(review_id=None)), "review")


def test_a_review_of_other_sql_is_unverified() -> None:
    log = [plan(), validated("val-1"), ran("val-1"), validated("val-2"), ran("val-2")]
    log.append(reviewed("rev-1", "val-1"))
    unverified(check(log, answer(validation_id="val-2")), "rev-1", "val-2")


def test_a_review_that_found_issues_is_unverified() -> None:
    log = [plan(), validated(), ran(), reviewed(passed=False)]
    unverified(check(log, answer()), "critic")


def test_steps_out_of_order_are_unverified() -> None:
    log = [plan(), validated(), reviewed(), ran()]
    unverified(check(log, answer()), "before")


# --- the critic off --------------------------------------------------------------------------


def test_with_the_critic_off_no_review_is_needed() -> None:
    assert check(FULL[:3], answer(review_id=None), critic=False).verified


def test_with_the_critic_off_the_rest_is_still_checked() -> None:
    unverified(check(FULL[:2], answer(review_id=None), critic=False), "run_sql")


# --- the intent ------------------------------------------------------------------------------


def test_an_information_answer_needs_only_a_plan() -> None:
    log = [plan(intent="information"), call("search_ontology", hits=[])]
    ans = answer(sql=None, validation_id=None, review_id=None, answer="Customer and Invoice.")
    assert check(log, ans).verified


def test_an_information_answer_still_needs_its_plan() -> None:
    ans = answer(sql=None, validation_id=None, review_id=None)
    unverified(check([plan("plan-2", intent="information")], ans), "plan-1")


def test_the_intent_is_the_answers_plans() -> None:
    log = [plan("plan-1", intent="information"), plan("plan-2"), validated(plan_id="plan-2")]
    unverified(check(log, answer(plan_id="plan-2", review_id=None)), "run_sql")


# --- answerable=false ------------------------------------------------------------------------


def test_an_unanswerable_answer_needs_a_plan_and_no_sql() -> None:
    ans = answer(
        answerable=False, sql=None, validation_id=None, review_id=None, missing=["refund dates"]
    )
    assert check([plan()], ans).verified


def test_an_unanswerable_answer_without_a_plan_is_unverified() -> None:
    ans = answer(answerable=False, sql=None, validation_id=None, review_id=None)
    unverified(check([], ans), "plan")


@pytest.mark.parametrize("intent", ["calculation", "information"])
def test_an_unanswerable_answer_with_sql_is_unverified(intent: str) -> None:
    ans = answer(answerable=False, validation_id=None, review_id=None)
    unverified(check([plan(intent=intent)], ans), "answerable=false", "SQL")
