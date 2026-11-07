"""The post-run check (ARCHITECTURE.md §8.1): after the session, the engine reads the tool log and
confirms the sequence plan → validate → run → review for the SQL the answer gives. If a step is
missing or an id doesn't match, the answer is shown as unverified with the reason; it is never
silently accepted.

What the sequence must hold depends on the session:
- a `calculation` (or any intent but `information`) needs all four steps for the final SQL;
- an `information` answer needs only its plan, and no SQL;
- with the critic off, no review is needed;
- an `answerable=false` answer needs its plan and no SQL;
- an `information` or `answerable=false` answer is unverified once run_sql has returned rows in
  the session, since it would rest on them unchecked.

The intent is the one the answer's plan recorded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ontological.answer.models import AnswerModel
from ontological.llm.gateway import ToolCall


@dataclass(frozen=True)
class Verdict:
    verified: bool
    reason: str | None = None  # why the answer is unverified


VERIFIED = Verdict(verified=True)


def post_run_check(log: Sequence[ToolCall], answer: AnswerModel | None, *, critic: bool) -> Verdict:
    if answer is None:
        return _unverified("the session ended with no answer")

    plans = _issued(log, "plan", "plan_id")
    if not plans:
        return _unverified("plan never ran, so nothing records what the question asks")
    if answer.plan_id not in plans:
        return _unverified(f"the answer's plan_id {answer.plan_id} isn't one plan issued")
    intent = plans[answer.plan_id][1].data.get("intent")
    sql = _normalized(answer.sql)

    # The model picks the intent and answerable, and can re-plan at any time, so neither can
    # vouch for SQL, nor for rows that run_sql returned (the tools don't withhold run_sql yet)
    if not answer.answerable or intent == "information":
        kind = (
            "an answer with answerable=false" if not answer.answerable else "an information answer"
        )
        if sql:
            return _unverified(f"{kind} carries SQL; it should carry none")
        if any(call.name == "run_sql" and not call.is_error for call in log):
            return _unverified(
                f"run_sql ran in this session, so {kind} rests on rows; it needs the SQL that "
                "returned them, validated, run and reviewed"
            )
        return VERIFIED

    if not sql:
        return _unverified("the answer has no SQL, and a calculation needs validated SQL")
    validation_id = answer.validation_id
    validations = _issued(log, "validate_sql", "validation_id")
    if validation_id is None or validation_id not in validations:
        return _unverified(f"{validation_id} isn't a validation_id validate_sql issued")
    validated = validations[validation_id][1]
    if _normalized(validated.data.get("sql")) != sql:
        return _unverified(f"the answer's SQL isn't the SQL validate_sql passed as {validation_id}")
    if validated.data.get("plan_id") != answer.plan_id:
        return _unverified(
            f"{validation_id} was validated under {validated.data.get('plan_id')}, "
            f"not under the answer's plan {answer.plan_id}"
        )

    runs = [
        i
        for i, call in enumerate(log)
        if call.name == "run_sql"
        and not call.is_error
        and call.data.get("validation_id") == validation_id
    ]
    if not runs:
        return _unverified(f"run_sql never ran {validation_id}")
    if not critic:
        return VERIFIED

    review_id = answer.review_id
    reviews = _issued(log, "review_answer", "review_id")
    if review_id is None:
        return _unverified(f"the critic is on and review_answer never reviewed {validation_id}")
    if review_id not in reviews:
        return _unverified(f"{review_id} isn't a review_id review_answer issued")
    at, review = reviews[review_id]
    if review.data.get("validation_id") != validation_id:
        return _unverified(
            f"{review_id} reviewed {review.data.get('validation_id')}, not the answer's "
            f"{validation_id}"
        )
    if at < runs[0]:
        return _unverified(f"the review {review_id} came before run_sql ran {validation_id}")
    if not review.data.get("passed"):
        issues = "; ".join(review.data.get("issues") or []) or "no issues named"
        return _unverified(f"the critic found issues with {validation_id}: {issues}")
    return VERIFIED


def _issued(log: Sequence[ToolCall], tool: str, key: str) -> dict[str, tuple[int, ToolCall]]:
    """The ids `tool` issued under `key`, with where in the log each was issued."""
    return {
        str(call.data[key]): (i, call)
        for i, call in enumerate(log)
        if call.name == tool and not call.is_error and call.data.get(key)
    }


def _normalized(sql: object) -> str:
    return str(sql or "").strip().rstrip(";").strip()


def _unverified(reason: str) -> Verdict:
    return Verdict(verified=False, reason=reason)
