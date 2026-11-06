"""The answer agent's tools and their preconditions (ARCHITECTURE.md §8.1, BUILD_PLAN.md M1.7),
called the way the SDK's MCP server calls them, with the ontology, the source and the critic
stood in for. Each tool checks what the session has done and refuses, naming the missing step,
when a step it needs hasn't happened."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from ontological.answer.gate import KeyColumns
from ontological.answer.models import Review
from ontological.answer.tools import (
    MAX_REVIEWS,
    MAX_VALIDATIONS,
    AnswerTools,
    ReviewInput,
    ToolResult,
)

from ontological.catalog.parsers.catalog_index import CatalogIndex, SchemaEntry, TableEntry
from ontological.sources.executor import Rows, SourceQueryError

QUESTION = "How many albums does each artist have?"
GOOD_SQL = (
    "SELECT ar.Name, COUNT(al.AlbumId) AS albums FROM Artist ar "
    "JOIN Album al ON al.ArtistId = ar.ArtistId GROUP BY ar.Name"
)
PLAN = {"intent": "calculation", "calc_subtype": "counting", "entities": ["artist", "album"]}

CATALOG = CatalogIndex(
    {
        "main": SchemaEntry(
            {
                "artist": TableEntry("t-artist", "Artist", {"artistid": "c1", "name": "c2"}),
                "album": TableEntry(
                    "t-album", "Album", {"albumid": "c3", "title": "c4", "artistid": "c5"}
                ),
            }
        )
    }
)


@dataclass
class FakeOntology:
    calls: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])

    def catalog(self, database: str) -> CatalogIndex:
        self.calls.append(("catalog", database))
        return CATALOG

    def key_columns(self, database: str, table: str) -> KeyColumns:
        return KeyColumns(pk=[f"{table.lower()}id"], unique=[])

    def search(self, database: str, query: str, kinds: Sequence[str]) -> list[dict[str, Any]]:
        self.calls.append(("search", database, query, *kinds))
        return [{"id": "a-1", "label": "ColumnAttribute", "name": "Artist Name", "score": 0.2}]

    def describe_table(self, database: str, table: str) -> dict[str, Any] | None:
        self.calls.append(("describe", database, table))
        if table not in ("t-artist", "Artist"):
            return None
        return {"id": "t-artist", "name": "Artist", "columns": [{"name": "Name"}]}


@dataclass
class FakeRunner:
    rows: Rows = field(
        default_factory=lambda: Rows(
            ["Name", "albums"], ["VARCHAR", "BIGINT"], [("AC/DC", 2), ("Accept", 2)], False
        )
    )
    error: str | None = None
    ran: list[str] = field(default_factory=list[str])

    def run_sql(self, sql: str) -> Rows:
        self.ran.append(sql)
        if self.error:
            raise SourceQueryError(self.error)
        return self.rows


@dataclass
class FakeCritic:
    verdicts: list[Review | None] = field(default_factory=list[Review | None])
    seen: list[ReviewInput] = field(default_factory=list[ReviewInput])

    async def __call__(self, review: ReviewInput) -> Review | None:
        self.seen.append(review)
        return self.verdicts.pop(0) if self.verdicts else Review(is_valid=True)


class Harness:
    def __init__(self, *, critic: bool = True, database: str = "chinook") -> None:
        self.ontology = FakeOntology()
        self.runner = FakeRunner()
        self.critic = FakeCritic()
        self.tools = AnswerTools(
            QUESTION,
            database=database,
            ontology=self.ontology,
            runner=self.runner,
            review=self.critic if critic else None,
        )

    def call(self, name: str, **args: Any) -> ToolResult:
        handler = next(t.handler for t in self.tools.sdk_tools() if t.name == name)
        return asyncio.run(handler(args))

    def planned(self) -> str:
        return data(self.call("plan", **PLAN))["plan_id"]

    def validated(self, sql: str = GOOD_SQL) -> str:
        self.planned()
        return data(self.call("validate_sql", sql=sql))["validation_id"]

    def ran(self, sql: str = GOOD_SQL) -> str:
        validation_id = self.validated(sql)
        assert not self.call("run_sql", validation_id=validation_id).get("is_error")
        return validation_id


def text(result: ToolResult) -> str:
    return "".join(c["text"] for c in result["content"])


def data(result: ToolResult) -> dict[str, Any]:
    assert not result.get("is_error"), text(result)
    return result["data"]


def refused(result: ToolResult, *needles: str) -> None:
    assert result.get("is_error") is True
    message = text(result)
    assert message.startswith("Refused:"), message
    for needle in needles:
        assert needle in message, message


# --- the tool set ----------------------------------------------------------------------------


def test_the_session_offers_the_six_tools() -> None:
    names = [t.name for t in Harness().tools.sdk_tools()]
    assert names == [
        "plan",
        "search_ontology",
        "describe_table",
        "validate_sql",
        "run_sql",
        "review_answer",
    ]


def test_with_the_critic_off_review_answer_is_not_offered() -> None:
    names = [t.name for t in Harness(critic=False).tools.sdk_tools()]
    assert "review_answer" not in names and len(names) == 5


# --- plan comes first -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("search_ontology", {"query": "artist"}),
        ("describe_table", {"id": "t-artist"}),
        ("validate_sql", {"sql": GOOD_SQL}),
        ("run_sql", {"validation_id": "val-1"}),
        ("review_answer", {"validation_id": "val-1", "answer_draft": "two each"}),
    ],
)
def test_every_tool_but_plan_refuses_before_a_plan(tool: str, args: dict[str, Any]) -> None:
    h = Harness()
    refused(h.call(tool, **args), "plan")
    assert h.ontology.calls == [] and h.runner.ran == [] and h.critic.seen == []


def test_plan_records_the_intent_and_the_database_and_issues_a_plan_id() -> None:
    h = Harness()
    plan = data(h.call("plan", **PLAN, evidence=["an artist has albums"]))
    assert plan["plan_id"] == "plan-1"
    assert plan["database"] == "chinook"
    assert plan["intent"] == "calculation" and plan["calc_subtype"] == "counting"
    assert plan["entities"] == ["artist", "album"]
    assert plan["evidence"] == ["an artist has albums"]


def test_plan_refuses_a_database_outside_the_scope() -> None:
    h = Harness()
    refused(h.call("plan", **PLAN, database="northwind"), "chinook")
    assert data(h.call("plan", **PLAN, database="chinook"))["plan_id"] == "plan-1"


def test_a_second_plan_gets_its_own_id() -> None:
    h = Harness()
    assert h.planned() == "plan-1"
    assert h.planned() == "plan-2"


# --- detail tools ----------------------------------------------------------------------------


def test_search_ontology_searches_the_plans_database() -> None:
    h = Harness()
    h.planned()
    hits = data(h.call("search_ontology", query="artist name", kinds=["attribute"]))["hits"]
    assert hits[0]["id"] == "a-1"
    assert ("search", "chinook", "artist name", "attribute") in h.ontology.calls


def test_describe_table_returns_the_table_or_says_it_is_not_there() -> None:
    h = Harness()
    h.planned()
    assert data(h.call("describe_table", id="t-artist"))["table"]["name"] == "Artist"
    missing = h.call("describe_table", id="t-nope")
    assert missing.get("is_error") and "t-nope" in text(missing)


# --- validate_sql ----------------------------------------------------------------------------


def test_validate_sql_passes_good_sql_with_an_id_bound_to_the_plan() -> None:
    h = Harness()
    h.planned()
    v = data(h.call("validate_sql", sql=GOOD_SQL))
    assert v["validation_id"] == "val-1" and v["ok"] is True
    assert v["plan_id"] == "plan-1" and v["sql"] == GOOD_SQL
    assert v["tables"] == ["main.Album", "main.Artist"]


def test_validate_sql_returns_the_gates_issues_and_no_id() -> None:
    h = Harness()
    h.planned()
    result = h.call("validate_sql", sql="SELECT Titel FROM Album")
    assert result.get("is_error") is True
    assert result["data"]["ok"] is False and "validation_id" not in result["data"]
    assert "titel" in text(result).lower()


def test_a_validation_after_a_new_plan_is_bound_to_the_new_plan() -> None:
    h = Harness()
    h.planned()
    h.planned()
    assert data(h.call("validate_sql", sql=GOOD_SQL))["plan_id"] == "plan-2"


def test_validate_sql_refuses_after_its_limit() -> None:
    h = Harness()
    h.planned()
    for _ in range(MAX_VALIDATIONS):
        h.call("validate_sql", sql="SELECT Titel FROM Album")
    refused(h.call("validate_sql", sql=GOOD_SQL), str(MAX_VALIDATIONS), "answerable")
    assert MAX_VALIDATIONS == 7  # upstream's cap on attempts


# --- run_sql ---------------------------------------------------------------------------------


def test_run_sql_refuses_an_id_validate_sql_did_not_issue() -> None:
    h = Harness()
    h.planned()
    h.call("validate_sql", sql="SELECT Titel FROM Album")  # failed: no id
    refused(h.call("run_sql", validation_id="val-1"), "validate_sql")
    assert h.runner.ran == []


def test_run_sql_runs_the_validated_sql_and_returns_its_rows() -> None:
    h = Harness()
    validation_id = h.validated()
    rows = data(h.call("run_sql", validation_id=validation_id))
    assert h.runner.ran == [GOOD_SQL]
    assert rows["validation_id"] == validation_id
    assert rows["columns"] == ["Name", "albums"]
    assert rows["rows"] == [["AC/DC", 2], ["Accept", 2]]
    assert rows["row_count"] == 2 and rows["truncated"] is False


def test_a_source_error_is_returned_for_the_model_to_fix() -> None:
    h = Harness()
    validation_id = h.validated()
    h.runner.error = "Binder Error: no such function: foo"
    result = h.call("run_sql", validation_id=validation_id)
    assert result.get("is_error") is True and "Binder Error" in text(result)
    assert not text(result).startswith("Refused:")
    # A failed run hasn't run: review still refuses it
    refused(h.call("review_answer", validation_id=validation_id, answer_draft="x"), "run_sql")


# --- review_answer ---------------------------------------------------------------------------


def test_review_answer_refuses_sql_that_has_not_run() -> None:
    h = Harness()
    validation_id = h.validated()
    refused(h.call("review_answer", validation_id=validation_id, answer_draft="x"), "run_sql")
    assert h.critic.seen == []


def test_review_answer_refuses_with_the_critic_off() -> None:
    h = Harness(critic=False)
    validation_id = h.ran()
    handler = h.tools.review_answer  # not offered to the model, but refuses if called
    refused(asyncio.run(handler({"validation_id": validation_id, "answer_draft": "x"})), "critic")


def test_review_answer_sends_the_question_plan_sql_and_rows_to_the_critic() -> None:
    h = Harness()
    validation_id = h.ran()
    review = data(h.call("review_answer", validation_id=validation_id, answer_draft="Two each."))
    assert review == {
        "review_id": "rev-1",
        "validation_id": validation_id,
        "passed": True,
        "reviewed": True,
        "issues": [],
        "reasoning": "",
    }
    [seen] = h.critic.seen
    assert seen.question == QUESTION and seen.sql == GOOD_SQL
    assert seen.plan["intent"] == "calculation"
    assert seen.columns == ["Name", "albums"] and seen.rows == [["AC/DC", 2], ["Accept", 2]]
    assert seen.tables == ["main.Album", "main.Artist"]
    assert seen.answer_draft == "Two each."


def test_the_critics_issues_come_back_for_the_model_to_fix() -> None:
    h = Harness()
    h.critic.verdicts = [Review(is_valid=False, aggregation_issues=["COUNT, not SUM"])]
    validation_id = h.ran()
    review = data(h.call("review_answer", validation_id=validation_id, answer_draft="x"))
    assert review["passed"] is False and review["issues"] == ["aggregation: COUNT, not SUM"]


def test_invalid_without_issues_passes_as_upstream() -> None:
    h = Harness()
    h.critic.verdicts = [Review(is_valid=False)]
    review = data(h.call("review_answer", validation_id=h.ran(), answer_draft="x"))
    assert review["passed"] is True


def test_a_critic_that_does_not_answer_passes_unreviewed_as_upstream() -> None:
    h = Harness()
    h.critic.verdicts = [None]
    review = data(h.call("review_answer", validation_id=h.ran(), answer_draft="x"))
    assert review["passed"] is True and review["reviewed"] is False


def test_review_answer_refuses_after_its_rounds() -> None:
    h = Harness()
    validation_id = h.ran()
    for _ in range(MAX_REVIEWS):
        data(h.call("review_answer", validation_id=validation_id, answer_draft="x"))
    refused(h.call("review_answer", validation_id=validation_id, answer_draft="x"), "2")
    assert MAX_REVIEWS == 2 and len(h.critic.seen) == 2
