"""Answers without Claude or a store, for the API's tests and the `ontological ask` CLI's.

Chinook is the one database with a source. Every question gets the same verified answer, from a
session that ran plan, a validate_sql the gate refused, a validate_sql that passed, run_sql and
review_answer, unless `critic=False`, when the review is left out and the answer is unverified
(as if the critic were on: a fake has no post-run check to vary). Each call is kept in `asked`.
`check` covers "artist" and misses "release year" in Chinook, or in no database for any other.
"""

from __future__ import annotations

from ontological.answer.answerable import Answerability, CoveringAttribute, EntityCoverage
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
        self.asked: list[tuple[str, str | None, bool]] = []
        self.checked: list[tuple[str, str | None]] = []

    async def check(self, question: str, database: str | None = None) -> Answerability:
        self.checked.append((question, database))
        known = database in (None, "chinook")
        artist = CoveringAttribute(
            id="attr-1",
            attribute="Name",
            term="Artist",
            table="Artist",
            column="Name",
            distance=0.41,
        )
        return Answerability(
            question=question,
            sanitized_question=question,
            extracted=True,
            database="chinook" if known else database,
            coverage=0.5 if known else 0.0,
            max_distance=0.671,
            entities=[
                EntityCoverage(
                    entity="artist",
                    covered=known,
                    nearest=0.41 if known else None,
                    attributes=[artist] if known else [],
                ),
                EntityCoverage(entity="release year", covered=False, nearest=0.71, attributes=[]),
            ],
            missing=["release year"] if known else ["artist", "release year"],
        )

    async def ask(self, question: str, database: str | None, *, critic: bool = True) -> Asked:
        self.asked.append((question, database, critic))
        database = database or "chinook"  # the one database, which covers every question
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
