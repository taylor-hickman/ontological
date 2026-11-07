"""The answer agent's tools and their preconditions (ARCHITECTURE.md §8.1, BUILD_PLAN.md M1.7,
M4.3),
called the way the SDK's MCP server calls them, with the ontology, the source and the critic
stood in for. Each tool checks what the session has done and refuses, naming the missing step,
when a step it needs hasn't happened."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from fakes import DuckProber, FakeGraph, frame

from ontological.answer.gate import KeyColumns
from ontological.answer.models import SUBTYPE_TEMPLATES, Review
from ontological.answer.pack import GroundingPack
from ontological.answer.probe import MAX_PROBES
from ontological.answer.tools import (
    MAX_REVIEWS,
    MAX_VALIDATIONS,
    SOFT_CHECKS_UNTIL,
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


def table(id: str, name: str, columns: list[str]) -> TableEntry:
    return TableEntry(
        id,
        name,
        {c.lower(): f"{id}.{c}" for c in columns},
        {c.lower(): c for c in columns},
    )


CATALOG = CatalogIndex(
    {
        "main": SchemaEntry(
            {
                "artist": table("t-artist", "Artist", ["ArtistId", "Name"]),
                "album": table("t-album", "Album", ["AlbumId", "Title", "ArtistId"]),
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

    def join_graph(self, database: str) -> FakeGraph:
        self.calls.append(("join_graph", database))
        return FakeGraph()

    def search(self, database: str, query: str, kinds: Sequence[str]) -> list[dict[str, Any]]:
        self.calls.append(("search", database, query, *kinds))
        return [{"id": "a-1", "label": "ColumnAttribute", "name": "Artist Name", "score": 0.2}]

    def describe_table(self, database: str, table: str) -> dict[str, Any] | None:
        self.calls.append(("describe", database, table))
        if table not in ("t-artist", "Artist"):
            return None
        return {"id": "t-artist", "name": "Artist", "columns": [{"name": "Name"}]}

    def get_term(self, database: str, term: str) -> dict[str, Any] | None:
        self.calls.append(("term", database, term))
        if term not in ("t-1", "Artist"):
            return None
        return {"id": "t-1", "name": "Artist", "synonyms": ["band"], "tables": ["Artist"]}

    def join_path(
        self, database: str, from_table: str, to_table: str
    ) -> list[dict[str, str]] | None:
        self.calls.append(("join", database, from_table, to_table))
        if {from_table, to_table} - {"Artist", "Album", "Genre"}:
            return None
        if "Genre" in (from_table, to_table):
            return []
        return [ALBUM_TO_ARTIST]

    def plan_pack(
        self,
        database: str,
        question: str,
        *,
        entities: Sequence[str],
        evidence: str,
        subject: str,
        anchor_attribute_id: str | None,
    ) -> GroundingPack:
        self.calls.append(
            (
                "plan_pack",
                database,
                question,
                *entities,
                evidence,
                subject,
                str(anchor_attribute_id),
            )
        )
        return PLAN_PACK


ALBUM_TO_ARTIST = {
    "source_table": "Album",
    "source_column": "ArtistId",
    "target_table": "Artist",
    "target_column": "ArtistId",
}
ARTIST_TABLE = {
    "id": "t-artist",
    "name": "Artist",
    "schema_name": "main",
    "columns": [{"name": "ArtistId"}, {"name": "Name", "description": "The artist's name."}],
}
ALBUM_TABLE = {
    "id": "t-album",
    "name": "Album",
    "schema_name": "main",
    "columns": [{"name": "AlbumId"}, {"name": "Title"}, {"name": "ArtistId"}],
}
ATTRIBUTE = {
    "id": "a-2",
    "attr_name": "Album Title",
    "col_name": "Title",
    "col_id": "c-title",
    "table_id": "t-album",
    "table_name": "Album",
    "schema_name": "main",
    "database_name": "chinook",
}
PLAN_PACK = GroundingPack(
    question=QUESTION,
    database="chinook",
    attributes=[ATTRIBUTE],
    terms=[],
    term_synonyms={},
    tables=[ARTIST_TABLE, ALBUM_TABLE],
    anchor={**ATTRIBUTE, "datatype": ""},
    sql_attributes=[
        {"id": "s-1", "name": "Album Count", "description": "Albums per artist.", "sql": "SELECT 1"}
    ],
)


@dataclass
class FakeRunner:
    rows: Rows = field(
        default_factory=lambda: Rows(
            ["Name", "albums"], ["VARCHAR", "BIGINT"], [("AC/DC", 2), ("Accept", 2)], False
        )
    )
    error: str | None = None
    ran: list[str] = field(default_factory=list[str])
    probed: list[str] = field(default_factory=list[str])

    def run_sql(self, sql: str) -> Rows:
        self.ran.append(sql)
        if self.error:
            raise SourceQueryError(self.error)
        return self.rows

    def probe(self, sql: str, *, max_rows: int) -> Rows:
        """Every probe reads the artists' names."""
        self.probed.append(sql)
        return frame(value=["AC/DC", "Accept"])


@dataclass
class FakeCritic:
    verdicts: list[Review | None] = field(default_factory=list[Review | None])
    seen: list[ReviewInput] = field(default_factory=list[ReviewInput])

    async def __call__(self, review: ReviewInput) -> Review | None:
        self.seen.append(review)
        return self.verdicts.pop(0) if self.verdicts else Review(is_valid=True)


class Harness:
    def __init__(
        self,
        *,
        critic: bool = True,
        database: str = "chinook",
        others: Sequence[str] = (),
        shown_tables: Sequence[str] = (),
    ) -> None:
        self.ontology = FakeOntology()
        self.runner: Any = FakeRunner()
        self.opened: list[str] = []
        self.critic = FakeCritic()
        self.tools = AnswerTools(
            QUESTION,
            database=database,
            others=others,
            ontology=self.ontology,
            runners=self.runners,
            review=self.critic if critic else None,
            shown_tables=shown_tables,
        )

    def runners(self, database: str) -> Any:
        self.opened.append(database)
        return self.runner

    def call(self, name: str, **args: Any) -> ToolResult:
        handler = next(t.handler for t in self.tools.sdk_tools() if t.name == name)

        async def go() -> ToolResult:
            return await handler(args)

        return asyncio.run(go())

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


def test_the_session_offers_the_nine_tools() -> None:
    names = [t.name for t in Harness().tools.sdk_tools()]
    assert names == [
        "plan",
        "search_ontology",
        "get_term",
        "describe_table",
        "join_path",
        "column_values",
        "validate_sql",
        "run_sql",
        "review_answer",
    ]


def test_with_the_critic_off_review_answer_is_not_offered() -> None:
    names = [t.name for t in Harness(critic=False).tools.sdk_tools()]
    assert "review_answer" not in names and len(names) == 8


# --- plan comes first -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("search_ontology", {"query": "artist"}),
        ("get_term", {"id": "t-1"}),
        ("describe_table", {"id": "t-artist"}),
        ("join_path", {"from_table": "Album", "to_table": "Artist"}),
        ("column_values", {"column": "Artist.Name"}),
        ("validate_sql", {"sql": GOOD_SQL}),
        ("run_sql", {"validation_id": "val-1"}),
        ("review_answer", {"validation_id": "val-1", "answer_draft": "two each"}),
    ],
)
def test_every_tool_but_plan_refuses_before_a_plan(tool: str, args: dict[str, Any]) -> None:
    h = Harness()
    refused(h.call(tool, **args), "plan")
    assert h.ontology.calls == [] and h.opened == [] and h.critic.seen == []


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


# --- plan's retrieval, the template and the intents (M4.3) -----------------------------------


def test_plan_retrieves_for_its_entities_in_the_plans_database() -> None:
    h = Harness()
    result = h.call(
        "plan",
        **PLAN,
        evidence=["albums are records", "an artist made them"],
        subject="artist",
        anchor_attribute_id="a-2",
    )
    assert h.ontology.calls == [
        (
            "plan_pack",
            "chinook",
            QUESTION,
            "artist",
            "album",
            "albums are records\nan artist made them",
            "artist",
            "a-2",
        )
    ]
    plan = data(result)
    assert plan["attribute_ids"] == ["a-2"] and plan["anchor"] == "a-2"
    assert plan["tables"] == ["Artist", "Album"] and plan["sql_attribute_ids"] == ["s-1"]
    message = text(result)
    assert "Candidates for the plan's entities" in message
    assert "Album.Title (attribute id a-2)" in message
    assert "Album Count: Albums per artist." in message and "SQL: SELECT 1" in message


def test_plan_names_tables_shown_already_without_listing_them_again() -> None:
    h = Harness(shown_tables=["t-artist"])
    first = text(h.call("plan", **PLAN))
    assert "main.Album (table id t-album)" in first
    assert "main.Artist (table id t-artist)" not in first
    assert "Also these, shown already: main.Artist" in first
    second = text(h.call("plan", **PLAN))
    assert "Also these, shown already: main.Artist, main.Album" in second


def test_a_calculation_plan_shows_its_subtypes_template() -> None:
    message = text(Harness().call("plan", **PLAN))
    assert "## The counting template" in message
    assert SUBTYPE_TEMPLATES["counting"] in message


def test_an_information_plan_has_no_template_and_withholds_run_sql_and_review() -> None:
    h = Harness()
    result = h.call("plan", intent="information", calc_subtype="counting", entities=["artist"])
    message = text(result)
    assert "template" not in message and "withheld" in message
    v = data(h.call("validate_sql", sql=GOOD_SQL))  # still allowed: it runs nothing
    refused(h.call("run_sql", validation_id=v["validation_id"]), "information", "withheld")
    refused(
        h.call("review_answer", validation_id=v["validation_id"], answer_draft="x"),
        "information",
    )
    assert h.runner.ran == []
    # Planned again as a calculation, the SQL can run
    h.call("plan", **PLAN)
    v = data(h.call("validate_sql", sql=GOOD_SQL))
    assert data(h.call("run_sql", validation_id=v["validation_id"]))["outcome"] == "rows"


def test_a_prediction_plan_is_told_it_is_unsupported_and_retrieves_nothing() -> None:
    h = Harness()
    result = h.call("plan", intent="prediction", entities=["sales next year"])
    plan = data(result)
    assert plan["plan_id"] == "plan-1" and plan["supported"] is False
    message = text(result)
    assert "Predictions aren't supported" in message
    assert "answerable=false" in message and "prediction_unsupported" in message
    assert h.ontology.calls == []
    for tool, args in [
        ("validate_sql", {"sql": GOOD_SQL}),
        ("run_sql", {"validation_id": "val-1"}),
        ("review_answer", {"validation_id": "val-1", "answer_draft": "x"}),
    ]:
        refused(h.call(tool, **args), "prediction", "withheld")
    assert h.opened == []


# --- a question with no scope ------------------------------------------------------------------


def test_without_a_scope_plan_may_name_another_ranked_database() -> None:
    h = Harness(others=["northwind", "chinook"])
    refused(h.call("plan", **PLAN, database="pagila"), "chinook, northwind")
    plan = data(h.call("plan", **PLAN, database="northwind"))
    assert plan["database"] == "northwind"
    assert h.ontology.calls[-1][:2] == ("plan_pack", "northwind")
    h.call("search_ontology", query="artist")
    assert h.ontology.calls[-1][:2] == ("search", "northwind")
    h.call("validate_sql", sql=GOOD_SQL)
    assert ("catalog", "northwind") in h.ontology.calls and h.opened == ["northwind"]
    # A plan that names no database keeps the latest one's
    assert data(h.call("plan", **PLAN))["database"] == "northwind"


def test_a_validation_runs_on_the_database_it_was_validated_in() -> None:
    h = Harness(others=["northwind"])
    validation_id = h.validated()
    h.call("plan", **PLAN, database="northwind")
    data(h.call("run_sql", validation_id=validation_id))
    assert h.opened == ["chinook", "chinook"]  # the probes' runner, then run_sql's


# --- detail tools ----------------------------------------------------------------------------


def test_get_term_returns_the_term_or_says_it_is_not_there() -> None:
    h = Harness()
    h.planned()
    assert data(h.call("get_term", id="Artist"))["term"]["synonyms"] == ["band"]
    missing = h.call("get_term", id="Nope")
    assert missing.get("is_error") and "Nope" in text(missing)


def test_join_path_returns_the_hops_or_says_there_are_none() -> None:
    h = Harness()
    h.planned()
    found = h.call("join_path", from_table="Album", to_table="Artist")
    assert data(found)["path"] == [ALBUM_TO_ARTIST]
    assert text(found) == "Album to Artist: Album.ArtistId = Artist.ArtistId"
    none = h.call("join_path", from_table="Album", to_table="Genre")
    assert data(none)["path"] == [] and "no join path" in text(none)
    unknown = h.call("join_path", from_table="Album", to_table="Nope")
    assert unknown.get("is_error") and "'Nope'" in text(unknown)


def values_runner() -> DuckRunner:
    return DuckRunner(
        "CREATE TABLE Artist (ArtistId INTEGER, Name VARCHAR)",
        "INSERT INTO Artist VALUES (1, 'AC/DC'), (2, 'Accept'), (3, 'AC/DC'), (4, NULL),"
        " (5, 'Aerosmith')",
    )


def test_column_values_reads_the_range_and_the_most_frequent_values() -> None:
    h = Harness()
    h.runner = values_runner()
    h.planned()
    got = data(h.call("column_values", column="artist.name"))
    assert got["column"] == "Artist.Name"
    assert got["values"][0] == {"value": "AC/DC", "rows": 2}
    assert [v["value"] for v in got["values"]] == ["AC/DC", "Accept", "Aerosmith"]
    assert (got["min"], got["max"], got["distinct_count"], got["nulls"]) == (
        "AC/DC",
        "Aerosmith",
        3,
        1,
    )
    assert got["more"] is False


def test_column_values_filters_by_like_ignoring_case_and_caps_the_values() -> None:
    h = Harness()
    h.runner = values_runner()
    h.planned()
    got = data(h.call("column_values", column="main.Artist.Name", like="ac%", limit=1))
    assert got["values"] == [{"value": "AC/DC", "rows": 2}] and got["more"] is True
    assert data(h.call("column_values", column="Artist.Name", like="%'%"))["values"] == []


def test_column_values_names_what_is_wrong_with_the_column() -> None:
    h = Harness()
    h.planned()
    for column, needle in [
        ("Name", "Table.Column"),
        ("Artist.Nme", "Its columns: ArtistId, Name"),
        ("Artists.Name", "no table 'Artists'"),
    ]:
        result = h.call("column_values", column=column)
        assert result.get("is_error") and needle in text(result), text(result)
    assert h.runner.probed == []


def test_column_values_shares_the_questions_probe_cap() -> None:
    h = Harness()
    h.runner = values_runner()
    h.planned()
    h.call("column_values", column="Artist.Name")
    probes = h.tools._probings["chinook"].probes  # pyright: ignore[reportPrivateUsage]
    assert probes.calls == 2
    probes.calls = MAX_PROBES
    result = h.call("column_values", column="Artist.Name")
    assert result.get("is_error") and "probe budget exhausted" in text(result)


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


NEAR_MISS_SQL = "SELECT Name FROM Artist WHERE Name = 'ac/dc'"


def test_validate_sql_probes_the_source_for_its_literals() -> None:
    h = Harness()
    h.planned()
    result = h.call("validate_sql", sql=NEAR_MISS_SQL)
    assert result.get("is_error") is True
    assert "Closest real value(s): AC/DC." in text(result)
    assert h.runner.probed and h.runner.ran == []


def test_after_five_calls_the_soft_checks_are_warnings() -> None:
    """As upstream: once 5 attempts have failed, the literal and case-dirty join checks stop
    blocking the SQL, and the model is told what they found instead."""
    h = Harness()
    h.planned()
    for _ in range(SOFT_CHECKS_UNTIL):
        assert h.call("validate_sql", sql=NEAR_MISS_SQL).get("is_error") is True
    result = h.call("validate_sql", sql=NEAR_MISS_SQL)
    v = data(result)
    assert v["ok"] is True and v["validation_id"] == "val-1"
    [warning] = v["warnings"]
    assert "AC/DC" in warning and "Warnings" in text(result)
    assert SOFT_CHECKS_UNTIL == 5  # upstream's INTENT_VALIDATION_SKIPPED_AFTER


def test_a_passing_validation_has_no_warnings() -> None:
    h = Harness()
    h.planned()
    assert data(h.call("validate_sql", sql=GOOD_SQL))["warnings"] == []


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


def test_rows_come_back_as_the_rows_outcome() -> None:
    h = Harness()
    assert data(h.call("run_sql", validation_id=h.validated()))["outcome"] == "rows"


# --- an empty result ---------------------------------------------------------------------------

# 25 artists, more than the literal check judges before the query runs (LOW_CARD_THRESHOLD)
ARTISTS = ["AC/DC", *(f"Artist {i:02}" for i in range(24))]
NEAR_MISS_HIGH_CARD = "SELECT Name FROM Artist WHERE Name = 'ac/dc'"


class DuckRunner(DuckProber):
    """A source on an in-memory DuckDB, for run_sql as well as the gate's probes."""

    def __init__(self, *setup: str) -> None:
        super().__init__(*setup)
        self.ran: list[str] = []

    def run_sql(self, sql: str) -> Rows:
        self.ran.append(sql)
        return Rows(**self.session.rows(sql, 200))


def chinook_runner() -> DuckRunner:
    names = ", ".join(f"({i + 1}, '{n}')" for i, n in enumerate(ARTISTS))
    return DuckRunner(
        "CREATE TABLE Artist (ArtistId INTEGER, Name VARCHAR)",
        f"INSERT INTO Artist VALUES {names}",
        "CREATE TABLE Album (AlbumId INTEGER, Title VARCHAR, ArtistId INTEGER)",
    )


def test_an_empty_result_is_its_own_outcome_and_asks_for_the_filters_to_be_rechecked() -> None:
    h = Harness()
    h.runner.rows = Rows(["Name", "albums"], ["VARCHAR", "BIGINT"], [], False)
    validation_id = h.validated()
    result = h.call("run_sql", validation_id=validation_id)
    ran = data(result)
    assert ran["outcome"] == "empty"
    assert ran["validation_id"] == validation_id and ran["row_count"] == 0 and ran["rows"] == []
    assert ran["columns"] == ["Name", "albums"]
    message = text(result)
    assert "no rows" in message and "filter" in message and "literal" in message
    assert validation_id in message
    # It ran: the empty result can be the answer, so review takes it
    assert data(h.call("review_answer", validation_id=validation_id, answer_draft="None."))


def test_an_empty_result_finds_a_near_miss_the_gate_left_alone() -> None:
    h = Harness()
    h.runner = chinook_runner()
    validation_id = h.validated(NEAR_MISS_HIGH_CARD)  # 25 names: the gate doesn't judge them
    result = h.call("run_sql", validation_id=validation_id)
    ran = data(result)
    assert ran["outcome"] == "empty"
    assert "AC/DC" in ran["repairs"] and "'ac/dc'" in ran["repairs"]
    assert "AC/DC" in text(result) and "validate_sql" in text(result)


def test_an_empty_result_with_nothing_to_repair_says_it_can_be_the_answer() -> None:
    h = Harness()
    h.runner = chinook_runner()
    sql = "SELECT Name FROM Artist WHERE Name = 'Nobody Like This'"
    result = h.call("run_sql", validation_id=h.validated(sql))
    ran = data(result)
    assert ran["outcome"] == "empty" and ran["repairs"] == ""
    assert "can be the answer" in text(result)


def test_the_empty_result_probes_share_the_questions_probe_cap() -> None:
    h = Harness()
    h.runner = chinook_runner()
    validation_id = h.validated(NEAR_MISS_HIGH_CARD)
    probes = h.tools._probings["chinook"].probes  # pyright: ignore[reportPrivateUsage]
    probes.calls = probes.max_calls  # the gate spent the question's budget
    ran = data(h.call("run_sql", validation_id=validation_id))
    assert ran["outcome"] == "empty" and ran["repairs"] == ""


def test_an_empty_result_says_its_like_filter_emptied_it() -> None:
    h = Harness()
    h.runner = chinook_runner()
    sql = "SELECT Name FROM Artist WHERE Name LIKE 'ac/dc%' AND ArtistId < 3"
    result = h.call("run_sql", validation_id=h.validated(sql))
    ran = data(result)
    assert ran["outcome"] == "empty"
    without_like = ran["without_like"]
    assert without_like["dropped"] == ["Name LIKE 'ac/dc%'"]
    assert without_like["columns"] == ["Name"]
    assert without_like["rows"] == [["AC/DC"], ["Artist 00"]]
    message = text(result)
    assert "LIKE filter" in message and "Name LIKE 'ac/dc%'" in message and "AC/DC" in message
    assert "validate_sql" in message


def test_an_empty_result_still_empty_without_its_like_filter_says_nothing_of_it() -> None:
    h = Harness()
    h.runner = chinook_runner()
    sql = "SELECT Name FROM Artist WHERE Name LIKE 'Nobody%' AND Name = 'Nobody At All'"
    result = h.call("run_sql", validation_id=h.validated(sql))
    ran = data(result)
    assert ran["outcome"] == "empty" and ran["without_like"] is None
    assert "LIKE filter" not in text(result) and "can be the answer" in text(result)


def test_the_like_probe_shares_the_questions_probe_cap() -> None:
    h = Harness()
    h.runner = chinook_runner()
    validation_id = h.validated("SELECT Name FROM Artist WHERE Name LIKE 'ac/dc%'")
    probes = h.tools._probings["chinook"].probes  # pyright: ignore[reportPrivateUsage]
    probes.calls = probes.max_calls
    ran = data(h.call("run_sql", validation_id=validation_id))
    assert ran["outcome"] == "empty" and ran["without_like"] is None


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
