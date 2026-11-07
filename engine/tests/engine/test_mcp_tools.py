"""The shim's eleven MCP tools (ARCHITECTURE.md §9.2, BUILD_PLAN.md M6.2), called in-process
through an MCP client, against the engine's real API with fake answers, ontology, sources and
Claude behind it. test_mcp_shim.py calls one over stdio through a real engine process."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
import pytest
from fake_answers import SQL, FakeAnswers
from fake_ontology import FakeOntology
from mcp import Client
from mcp.types import CallToolResult

from ontological.api.app import create_app
from ontological.catalog import sources as catalog
from ontological.catalog.sources import ColumnOut, SourceOut, TableDetail
from ontological.curate.ontology import (
    AttributeOut,
    Glossary,
    JoinOut,
    SqlAttributeDetail,
    SqlAttributeOut,
    SqlAttributeRef,
    TableRef,
    TableSemantics,
    TermDetail,
    TermRef,
    TermRow,
)
from ontological.engine.lifetime import Lifetime
from ontological.llm import install
from ontological.llm.install import ClaudeInstall, InstallState
from ontological.mcp import tools
from ontological.mcp.link import EngineUnavailable
from ontological.mcp.tools import build_server

TOKEN = "t" * 43
ELEVEN = {
    "check_readiness",
    "search_terms",
    "get_term",
    "get_term_columns",
    "get_term_sql_attributes",
    "get_sql_attribute",
    "describe_table",
    "check_answerable",
    "ask_question",
    "validate_sql",
    "run_validated_sql",
}

ARTIST = TermRef(id="term-artist", name="Artist")
ALBUM = TermRef(id="term-album", name="Album")
ARTIST_TABLE = TableRef(id="tbl-artist", name="Artist", schema_name="main", database_name="chinook")
TOP_ARTIST = SqlAttributeOut(
    id="sql-top",
    name="Albums per artist",
    description="How many albums each artist has.",
    expression="COUNT(Album.AlbumId)",
    source="sql",
    certified=True,
    uses=3,
)


def source(id: str, name: str, tables: int) -> SourceOut:
    return SourceOut(
        id=id,
        name=name,
        kind="sqlite",
        label=f"/data/{name}.db",
        size_bytes=1,
        schemas=1,
        tables=tables,
        columns=tables * 3,
        pulled=datetime(2026, 11, 7, tzinfo=UTC),
    )


def scenario() -> dict[str, Any]:
    """FakeOntology's ontology.json: Chinook compiled with two Terms, Sakila not."""
    artist = TermDetail(
        id=ARTIST.id,
        name=ARTIST.name,
        kind="entity",
        description="A person or band that records albums.",
        synonyms=["band", "musician"],
        name_certified=True,
        description_certified=False,
        created=None,
        tables=[ARTIST_TABLE],
        attributes=[
            AttributeOut(
                id="attr-name",
                name="Artist Name",
                column="Name",
                table="Artist",
                datatype="VARCHAR",
                description="The artist's name.",
                certified=False,
                joins_to=None,
            )
        ],
        joins=[
            JoinOut(table="Album", column="ArtistId", to=ALBUM),
            JoinOut(table="Album", column="ArtistId2", to=ALBUM),
        ],
        sql_attributes=[TOP_ARTIST],
        history=[],
        undoable=None,
    )
    row = TermRow(
        id=ARTIST.id,
        name=ARTIST.name,
        kind="entity",
        description=artist.description,
        synonyms=artist.synonyms,
        name_certified=True,
        description_certified=False,
        certified=False,
        tables=["Artist"],
        attributes=1,
        sql_attributes=1,
        changed_at=None,
    )
    glossary = Glossary(
        source_id="src-chinook", database_name="chinook", terms=[row], certified=1, total=2
    )
    detail = SqlAttributeDetail(
        **TOP_ARTIST.model_dump(),
        sql=SQL,
        term=ARTIST,
        tables=[ARTIST_TABLE],
    )
    semantics = TableSemantics(
        table_id=ARTIST_TABLE.id,
        terms=[ARTIST],
        sql_attributes=[
            SqlAttributeRef(id=TOP_ARTIST.id, name=TOP_ARTIST.name, description=None, term=ARTIST)
        ],
    )
    return {
        "glossaries": {"src-chinook": glossary.model_dump(mode="json")},
        "terms": {ARTIST.id: artist.model_dump(mode="json")},
        "sql_attributes": {TOP_ARTIST.id: detail.model_dump(mode="json")},
        "tables": {ARTIST_TABLE.id: semantics.model_dump(mode="json")},
    }


class Sources:
    def __init__(self, sources: list[SourceOut]) -> None:
        self.sources = sources

    def list(self) -> list[SourceOut]:
        return self.sources

    def table(self, table_id: str) -> TableDetail:
        return TableDetail(
            id=table_id,
            source_id="src-chinook",
            source="chinook",
            schema_name="main",
            name="Artist",
            table_type="BASE TABLE",
            description="Recording artists.",
            columns=[
                ColumnOut(
                    id="col-name",
                    name="Name",
                    data_type="VARCHAR",
                    is_nullable=True,
                    is_unique=False,
                    format=None,
                    sample_values=["AC/DC", "Accept"],
                    description="The artist's name.",
                    pk=False,
                    references=[],
                    tags=[],
                    pii=False,
                )
            ],
            tags=[],
        )


class Claude:
    def __init__(self, state: InstallState) -> None:
        self.state: InstallState = state

    async def detect(self) -> ClaudeInstall:
        return ClaudeInstall(
            state=self.state,
            cli_path=None,
            version="2.1.0",
            min_version="2.0.0",
            logged_in=self.state == "ready",
            ignored_variables=[],
        )


class AppEngine:
    """The engine's API in this process, as `EngineLink.call` reaches a live one."""

    def __init__(self, app: Any) -> None:
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.calls: list[tuple[str, str, Any, Any]] = []

    async def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float,
    ) -> httpx.Response:
        self.calls.append((method, path, body, params))
        return await self.http.request(method, path, json=body, params=params, timeout=timeout)


class World:
    def __init__(
        self,
        tmp_path: Path,
        *,
        sources: list[SourceOut] | None = None,
        claude: InstallState = "ready",
        glossaries: bool = True,
    ) -> None:
        data = scenario()
        if not glossaries:
            data["glossaries"] = {}
        (tmp_path / "ontology.json").write_text(json.dumps(data))
        self.answers = FakeAnswers()
        app = create_app(
            token=TOKEN,
            lifetime=Lifetime(idle_timeout_s=600, renew_every_s=30),
            pid=1,
            version="9.9.9",
            answers=self.answers,
            ontology=FakeOntology(tmp_path),
            sources=cast(
                catalog.Sources,
                Sources([source("src-chinook", "chinook", 11)] if sources is None else sources),
            ),
            claude=cast(install.Claude, Claude(claude)),
        )
        self.engine = AppEngine(app)
        self.server = build_server(self.engine)


Session = Callable[[Client], Coroutine[Any, Any, None]]


def run(world: World, session: Session) -> None:
    async def go() -> None:
        async with Client(world.server) as client:
            await session(client)

    anyio.run(go)


async def call(client: Client, name: str, **args: Any) -> dict[str, Any]:
    result = await client.call_tool(name, args)
    assert not result.is_error, text(result)
    assert result.structured_content is not None
    return result.structured_content


async def refused(client: Client, name: str, **args: Any) -> str:
    result = await client.call_tool(name, args)
    assert result.is_error
    return text(result)


def text(result: CallToolResult) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


# --- the surface ------------------------------------------------------------------------------


def test_the_server_offers_the_eleven_tools_all_read_only(world: World) -> None:
    async def session(client: Client) -> None:
        listed = (await client.list_tools()).tools
        assert {t.name for t in listed} == ELEVEN
        for t in listed:
            assert t.annotations is not None and t.annotations.read_only_hint, t.name
            assert t.annotations.destructive_hint is False, t.name
            assert t.description and "Auto Ontology" not in t.description, t.name
        by_name = {t.name: t for t in listed}
        assert by_name["validate_sql"].input_schema["required"] == ["sql"]
        assert by_name["run_validated_sql"].input_schema["required"] == ["validation_id"]

    run(world, session)
    assert "validate_sql" in tools.INSTRUCTIONS and "check_readiness" in tools.INSTRUCTIONS


# --- check_readiness --------------------------------------------------------------------------


def test_ready_with_claude_and_a_compiled_database(world: World) -> None:
    async def session(client: Client) -> None:
        got = await call(client, "check_readiness")
        assert got["ready"] is True and got["blockers"] == [] and got["notes"] == []
        assert got["claude"] == {"state": "ready", "logged_in": True}
        assert got["databases"] == [
            {
                "database": "chinook",
                "source_id": "src-chinook",
                "kind": "sqlite",
                "tables": 11,
                "terms": 2,
                "certified_terms": 1,
            }
        ]

    run(world, session)


def test_not_ready_with_no_source(tmp_path: Path) -> None:
    async def session(client: Client) -> None:
        got = await call(client, "check_readiness")
        assert got["ready"] is False and got["databases"] == []
        [blocker] = got["blockers"]
        assert "No source is added" in blocker

    run(World(tmp_path, sources=[]), session)


def test_not_ready_when_nothing_is_compiled_or_claude_needs_a_login(tmp_path: Path) -> None:
    async def session(client: Client) -> None:
        got = await call(client, "check_readiness")
        assert got["ready"] is False
        claude, compile_ = got["blockers"]
        assert "needs login" in claude and "validate_sql" in claude
        assert "compiled" in compile_ and "Jobs" in compile_
        assert got["databases"][0]["terms"] == 0

    run(World(tmp_path, claude="needs_login", glossaries=False), session)


def test_a_database_not_compiled_or_with_no_tables_is_a_note(tmp_path: Path) -> None:
    sources = [
        source("src-chinook", "chinook", 11),
        source("src-sakila", "sakila", 16),
        source("src-empty", "empty", 0),
    ]

    async def session(client: Client) -> None:
        got = await call(client, "check_readiness")
        assert got["ready"] is True
        empty, sakila = sorted(got["notes"])
        assert empty.startswith("empty has no tables")
        assert sakila.startswith("sakila isn't compiled")

    run(World(tmp_path, sources=sources), session)


# --- the glossary -----------------------------------------------------------------------------


def test_search_terms_passes_the_query_and_omits_what_isnt_given(world: World) -> None:
    async def session(client: Client) -> None:
        every = await call(client, "search_terms", query="musicians")
        assert [t["name"] for t in every["terms"]] == ["Artist", "Album"]
        one = await call(client, "search_terms", query="musicians", database="chinook", limit=1)
        assert [t["name"] for t in one["terms"]] == ["Artist"]

    run(world, session)
    searches = [c for c in world.engine.calls if c[1] == "/terms"]
    assert [c[3] for c in searches] == [
        {"q": "musicians", "limit": 10},
        {"q": "musicians", "database": "chinook", "limit": 1},
    ]


def test_get_term_leaves_its_columns_and_sql_to_their_own_tools(world: World) -> None:
    async def session(client: Client) -> None:
        got = await call(client, "get_term", term_id=ARTIST.id)
        assert got["name"] == "Artist" and got["synonyms"] == ["band", "musician"]
        assert got["tables"] == [ARTIST_TABLE.model_dump()]
        assert got["related_terms"] == [ALBUM.model_dump()]  # one, though two columns join it
        assert (got["columns"], got["sql_attributes"]) == (1, 1)
        assert "history" not in got and "attributes" not in got

        columns = await call(client, "get_term_columns", term_id=ARTIST.id)
        assert columns["term"] == ARTIST.model_dump()
        assert [c["column"] for c in columns["columns"]] == ["Name"]
        assert len(columns["joins"]) == 2

        sql = await call(client, "get_term_sql_attributes", term_id=ARTIST.id)
        assert sql["sql_attributes"] == [TOP_ARTIST.model_dump()]

        assert "no Term nope" in await refused(client, "get_term", term_id="nope")

    run(world, session)


def test_get_sql_attribute_has_its_sql_term_and_tables(world: World) -> None:
    async def session(client: Client) -> None:
        got = await call(client, "get_sql_attribute", attr_id=TOP_ARTIST.id)
        assert got["sql"] == SQL and got["term"] == ARTIST.model_dump()
        assert got["tables"] == [ARTIST_TABLE.model_dump()]
        message = await refused(client, "get_sql_attribute", attr_id="nope")
        assert "404" in message and "no SQL attribute nope" in message

    run(world, session)


def test_describe_table_adds_its_terms_and_sql_attributes_to_its_columns(world: World) -> None:
    async def session(client: Client) -> None:
        got = await call(client, "describe_table", table_id=ARTIST_TABLE.id)
        assert got["name"] == "Artist" and got["columns"][0]["sample_values"] == ["AC/DC", "Accept"]
        assert got["terms"] == [ARTIST.model_dump()]
        assert [a["id"] for a in got["sql_attributes"]] == [TOP_ARTIST.id]

    run(world, session)


# --- answers ----------------------------------------------------------------------------------


def test_check_answerable_returns_the_coverage(world: World) -> None:
    async def session(client: Client) -> None:
        got = await call(client, "check_answerable", question="Artists by release year?")
        assert (got["coverage"], got["missing"]) == (0.5, ["release year"])

    run(world, session)
    assert world.answers.checked == [("Artists by release year?", None)]


def test_ask_question_returns_the_answer_sql_rows_and_steps(world: World) -> None:
    async def session(client: Client) -> None:
        got = await call(client, "ask_question", question="Who has the most albums?")
        assert got["answer"].startswith("Iron Maiden")
        assert got["verified"] is True and got["unverified_because"] is None
        assert (got["database"], got["sql"]) == ("chinook", SQL)
        assert got["columns"] == ["Name", "albums"] and got["rows"][0] == ["Iron Maiden", 21]
        assert got["steps"] == [
            "plan",
            "validate_sql: failed",
            "validate_sql",
            "run_sql",
            "review_answer",
        ]

    run(world, session)
    assert world.answers.asked == [("Who has the most albums?", None, True)]


def test_ask_question_for_a_database_with_no_source_is_refused(world: World) -> None:
    async def session(client: Client) -> None:
        message = await refused(client, "ask_question", question="q", database="nope")
        assert "no source for nope" in message

    run(world, session)


# --- the gate ---------------------------------------------------------------------------------


def test_sql_is_validated_then_run_and_the_one_database_needs_no_naming(world: World) -> None:
    async def session(client: Client) -> None:
        bad = await call(client, "validate_sql", sql="SELECT * FROM Albums")
        assert bad["ok"] is False and bad["validation_id"] is None
        assert "Did you mean Album?" in bad["issues"][0]
        good = await call(client, "validate_sql", sql=SQL)
        assert good["ok"] is True and good["database"] == "chinook"
        ran = await call(client, "run_validated_sql", validation_id=good["validation_id"])
        assert ran["outcome"] == "rows" and ran["rows"][0] == ["Iron Maiden", 21]

    run(world, session)


def test_with_two_sources_validate_sql_asks_which(tmp_path: Path) -> None:
    sources = [source("src-chinook", "chinook", 11), source("src-sakila", "sakila", 16)]

    async def session(client: Client) -> None:
        message = await refused(client, "validate_sql", sql=SQL)
        assert "Name the database: one of chinook, sakila" in message
        assert (await call(client, "validate_sql", sql=SQL, database="chinook"))["ok"]

    run(World(tmp_path, sources=sources), session)


def test_the_soft_checks_warn_after_five_failures_in_a_row(world: World) -> None:
    near_miss = "SELECT Name FROM Artist WHERE Name = 'ac/dc'"

    async def session(client: Client) -> None:
        for _ in range(5):
            assert not (await call(client, "validate_sql", sql=near_miss))["ok"]
        sixth = await call(client, "validate_sql", sql=near_miss)
        assert sixth["ok"] and "AC/DC" in sixth["warnings"][0]
        # A pass starts the count again
        assert not (await call(client, "validate_sql", sql=near_miss))["ok"]

    run(world, session)
    flags = [c[2]["soft_as_warnings"] for c in world.engine.calls if c[1] == "/sql/validate"]
    assert flags == [False] * 5 + [True, False]


def test_an_empty_result_says_what_to_recheck(world: World) -> None:
    async def session(client: Client) -> None:
        sql = "SELECT Name FROM Artist WHERE Name = 'Nobody'"
        validated = await call(client, "validate_sql", sql=sql)
        ran = await call(client, "run_validated_sql", validation_id=validated["validation_id"])
        assert ran["outcome"] == "empty" and ran["rows"] == [] and "Recheck" in ran["note"]

    run(world, session)


def test_run_refuses_an_id_the_engine_did_not_issue(world: World) -> None:
    async def session(client: Client) -> None:
        message = await refused(client, "run_validated_sql", validation_id="val-9")
        assert "validate the SQL again" in message

    run(world, session)


# --- no engine --------------------------------------------------------------------------------


class NoEngine:
    async def call(self, method: str, path: str, **_: Any) -> httpx.Response:
        raise EngineUnavailable("no engine took a lease after 5 attempts")


def test_with_no_engine_a_tool_says_so() -> None:
    async def go() -> None:
        async with Client(build_server(NoEngine())) as client:
            message = await refused(client, "search_terms", query="x")
            assert "engine isn't available" in message and "5 attempts" in message

    anyio.run(go)
