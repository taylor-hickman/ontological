"""Execution accuracy by BIRD's rule (BUILD_PLAN.md, Scoring and runs).

Ported from ontology-sql-eval's verifier (`ontology_sql_eval/gym/exec_match.py` at `ec41b2c`,
Apache-2.0): a prediction matches when `set(gold) == set(pred)`, which ignores row order and
collapses duplicate rows, as BIRD's own `evaluation_ex.py` does. Unhashable cells fall back to a
sorted repr compare. A question that scores zero says why (`Failure`), so a rate-limited run
can't pass for a weak one: ontology-sql-eval added its `no_model_output` code after a
66%-rate-limited run reported a plausible 13.3%, and `report` keeps its 5% guard.

What differs from the port:
- Gold runs natively (`sets.GoldRunner`: sqlite3 for BIRD, the eval cluster for WWI) and the
  agent's SQL through Ontological's source executor, on DuckDB. Each query gets its own time
  limit instead of BIRD's shared one; every gold query in the sets runs well inside it (M0.8).
  A gold query that fails anyway scores zero and stays in the denominator, as BIRD scores it.
- **Dialect artifacts.** SQLite, Postgres and DuckDB return some answers as different types, so
  a mismatch whose rows agree under the tolerance rule (`normalized`) scores as a match and is
  flagged `dialect_artifact`, so the report lists it. The rule came from M1.10's hand review of
  what the first smoke runs flagged (BUILD_PLAN.md, Scoring and runs): dates and times compare
  as ISO text (SQLite keeps them as text, DuckDB reads them as DATE), whole numbers compare
  exactly, and other numbers to 6 significant digits (DuckDB's `CAST(x AS REAL)` is float32;
  Postgres's numeric against DuckDB's double). Rounding can split two values on either side of
  a 6-digit boundary, which leaves a true artifact a miss, never a wrong answer a match.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Rows = Sequence[Sequence[Any]]

# Why a question scored zero. None when it matched.
Failure = Literal[
    "no_answer",  # the session ended without an answer: a rate limit, an overload, an error
    "no_sql",  # an answer with no SQL, such as answerable=false
    "pred_error",  # the predicted SQL failed or timed out
    "pred_truncated",  # it returned more rows than the verifier fetches
    "gold_error",  # the gold SQL failed: scored zero, kept in the denominator
    "wrong_result",
]
SAMPLE_ROWS = 5  # rows of each side kept for a mismatch, for review


@dataclass(frozen=True)
class Scored:
    match: bool
    failure: Failure | None
    dialect_artifact: bool = False
    gold_rows: int | None = None
    pred_rows: int | None = None
    error: str | None = None
    gold_sample: list[list[Any]] = field(default_factory=list[list[Any]])
    pred_sample: list[list[Any]] = field(default_factory=list[list[Any]])

    def to_json(self) -> dict[str, Any]:
        return {
            "match": self.match,
            "failure": self.failure,
            "dialect_artifact": self.dialect_artifact,
            "gold_rows": self.gold_rows,
            "pred_rows": self.pred_rows,
            "error": self.error,
            "gold_sample": self.gold_sample,
            "pred_sample": self.pred_sample,
        }


def result_sets_match(gold: Rows, pred: Rows) -> bool:
    """BIRD's comparison: unordered set equality over row tuples."""
    try:
        return {tuple(r) for r in gold} == {tuple(r) for r in pred}
    except TypeError:
        # Unhashable cells (lists from DuckDB, say): a sorted repr compare.
        try:
            return sorted(repr(tuple(r)) for r in gold) == sorted(repr(tuple(r)) for r in pred)
        except Exception:
            return False


def normalize(value: Any) -> Any:
    """A cell as the tolerance rule compares it."""
    if isinstance(value, bool | int):
        return int(value)
    if isinstance(value, float | decimal.Decimal):
        f = float(value)
        if not math.isfinite(f):
            return f  # NaN and the infinities stay as they are
        if value == int(value):
            return int(value)  # a whole number is compared exactly
        return float(f"{f:.6g}")
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    return value


def normalized(rows: Rows) -> list[tuple[Any, ...]]:
    return [tuple(normalize(v) for v in row) for row in rows]


def score(
    gold: Rows | Exception,
    pred: Rows | Exception | None,
    *,
    pred_truncated: bool = False,
    no_answer: bool = False,
) -> Scored:
    """Score one prediction. `pred` is None when there is no SQL to run, and `no_answer` says
    the session gave no answer at all; an Exception on either side is its query's failure."""
    gold_n = None if isinstance(gold, Exception) else len(gold)
    if pred is None:
        return Scored(False, "no_answer" if no_answer else "no_sql", gold_rows=gold_n)
    if isinstance(pred, Exception):
        return Scored(False, "pred_error", gold_rows=gold_n, error=_message(pred))
    if pred_truncated:
        return Scored(False, "pred_truncated", gold_rows=gold_n, pred_rows=len(pred))
    if isinstance(gold, Exception):
        return Scored(False, "gold_error", pred_rows=len(pred), error=_message(gold))
    if result_sets_match(gold, pred):
        return Scored(True, None, gold_rows=len(gold), pred_rows=len(pred))
    tolerated = result_sets_match(normalized(gold), normalized(pred))
    return Scored(
        tolerated,
        None if tolerated else "wrong_result",
        dialect_artifact=tolerated,
        gold_rows=len(gold),
        pred_rows=len(pred),
        gold_sample=sample(gold),
        pred_sample=sample(pred),
    )


def sample(rows: Rows, n: int = SAMPLE_ROWS) -> list[list[Any]]:
    """The first n rows, JSON-safe."""
    return [[jsonable(v) for v in row] for row in list(rows)[:n]]


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _message(e: Exception) -> str:
    text = str(e).strip()
    return (text.splitlines()[0] if text else type(e).__name__)[:300]
