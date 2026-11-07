"""Answers without Claude or a store, for the API's tests and the `ontological ask` CLI's.

Chinook is the one database with a source. Every question gets the same verified answer, from a
session that ran plan, a validate_sql the gate refused, a validate_sql that passed, run_sql and
review_answer, unless `critic=False`, when the review is left out and the answer is unverified
(as if the critic were on: a fake has no post-run check to vary). Each call is kept in `asked`.
"""

from __future__ import annotations

from ontological.answer.models import AnswerModel
from ontological.answer.service import Asked, NoSource, Ran, Step

SQL = (
    "SELECT ar.Name, COUNT(al.AlbumId) AS albums FROM Artist ar "
    "JOIN Album al ON al.ArtistId = ar.ArtistId GROUP BY ar.Name ORDER BY albums DESC LIMIT 2"
)
RAN = Ran(
    columns=["Name", "albums"],
    types=["VARCHAR", "BIGINT"],
    rows=[["Iron Maiden", 21], ["Led Zeppelin", 14]],
    truncated=False,
)


def steps(*, critic: bool) -> list[Step]:
    plan = {"intent": "calculation", "calc_subtype": "ranking", "entities": ["artist", "albums"]}
    return [
        Step(name="plan", args=plan, is_error=False, text="{}", data={"plan_id": "plan-1"}),
        Step(
            name="validate_sql",
            args={"sql": "SELECT * FROM Albums"},
            is_error=True,
            text="Albums isn't a table in chinook. Did you mean Album?",
            data={"ok": False},
        ),
        Step(
            name="validate_sql",
            args={"sql": SQL},
            is_error=False,
            text="{}",
            data={"ok": True, "validation_id": "val-1", "sql": SQL},
        ),
        Step(
            name="run_sql",
            args={"validation_id": "val-1"},
            is_error=False,
            text="{}",
            data={"validation_id": "val-1", **RAN.model_dump(), "row_count": 2},
        ),
        *(
            [
                Step(
                    name="review_answer",
                    args={"validation_id": "val-1", "answer_draft": "Iron Maiden."},
                    is_error=False,
                    text="{}",
                    data={"review_id": "rev-1", "validation_id": "val-1", "passed": True},
                )
            ]
            if critic
            else []
        ),
    ]


class FakeAnswers:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str, bool]] = []

    async def ask(self, question: str, database: str, *, critic: bool = True) -> Asked:
        self.asked.append((question, database, critic))
        if database != "chinook":
            raise NoSource(database)
        return Asked(
            question=question,
            database=database,
            answer=AnswerModel(
                answer="Iron Maiden has the most albums, 21, then Led Zeppelin with 14.",
                sql=SQL,
                plan_id="plan-1",
                validation_id="val-1",
                review_id="rev-1" if critic else None,
                answerable=True,
            ),
            verified=critic,
            reason=None if critic else "review_answer never ran val-1",
            sql=SQL,
            result=RAN,
            steps=steps(critic=critic),
            outcome="ok",
            error=None,
            answered_model="claude-opus-5-5",
        )
