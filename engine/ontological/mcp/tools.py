"""The shim's MCP tools (ARCHITECTURE.md §9.2, BUILD_PLAN.md M6.2): upstream's curated nine, and
`validate_sql` and `run_validated_sql` for a calling Claude that writes its own SQL.

Each tool is one or a few requests to the engine's API, through whatever `Engine` the server is
built with: the shim's `EngineLink`, which waits for the engine it attaches to, or the app itself
in the tests. Upstream generates its tools from its OpenAPI spec; the engine publishes none, so
these are written out, with upstream's names and descriptions rebranded, and its routes mapped
to the engine's:

| Tool | Engine |
|---|---|
| `check_readiness` | `GET /claude`, `GET /sources`, and each source's `GET /sources/{id}/terms` |
| `search_terms` | `GET /terms?q=` (hybrid search; upstream filters names) |
| `get_term`, `get_term_columns`, `get_term_sql_attributes` | `GET /terms/{id}`, in three cuts |
| `get_sql_attribute` | `GET /sql-attributes/{id}` |
| `describe_table` | `GET /tables/{id}` and `GET /tables/{id}/semantics` |
| `check_answerable` | `POST /answerable` |
| `ask_question` | `POST /ask`, answered whole, with no progress notifications |
| `validate_sql`, `run_validated_sql` | `POST /sql/validate`, `POST /sql/run` |

Every tool reads: none changes the ontology, the catalog or a source, so each is annotated
read-only. `ask_question` and `check_answerable` use Claude quota; the rest don't.

`validate_sql` counts the SQL that fails for each database in a row, and from the
`SOFT_CHECKS_UNTIL`th on asks the engine for the soft checks as warnings, as the agent's session
does after that many calls, so a soft check that misfires can't block a caller for good.
"""

from __future__ import annotations

from typing import Annotated, Any, Protocol

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ontological.answer.tools import SOFT_CHECKS_UNTIL
from ontological.mcp.link import EngineUnavailable

READ_TIMEOUT_S = 60.0  # a store read, a hybrid search, or the gate's probes
CHECK_TIMEOUT_S = 180.0  # check_answerable: one Haiku call, which may queue for the slot
ASK_TIMEOUT_S = 1800.0  # ask_question: one or two agent sessions and their reviews
RUN_TIMEOUT_S = 600.0  # run_validated_sql: the query, and probes when it comes back empty

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


class Engine(Protocol):
    async def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float,
    ) -> httpx.Response: ...


INSTRUCTIONS = """\
Ontological answers questions about the person's own databases. It holds a compiled semantic \
layer over each source they added: a glossary of business Terms mapped onto real columns, and \
SQL attributes, reviewed SQL expressions under a Term.

There are two ways to get an answer from the data.

- `ask_question` runs Ontological's own gated text-to-SQL agent and returns the answer, the SQL \
it ran, the rows, and whether every step was verified. It is slow (a minute or more) and uses \
Claude quota.
- `validate_sql` and `run_validated_sql` let you write the SQL yourself. `validate_sql` runs \
Ontological's gate: one read-only SELECT, every table and column in the catalog, nothing \
degenerate, joins the ontology bears out, filter values the source holds. It returns the issues \
to fix, or a validation_id; `run_validated_sql` runs only SQL that passed. Prefer this when you \
can reason about the schema yourself: it costs no quota. When a query comes back empty, recheck \
its filters with the fixes it suggests before taking the empty result as the answer.

Either way, learn the vocabulary first:

1. `search_terms` finds what a business word means here. "Active customer" may be a defined \
Term with specific columns or SQL behind it, not something to guess at.
2. `get_term`, `get_term_columns` and `get_term_sql_attributes` go from a Term to the columns \
and reviewed expressions behind it; `get_sql_attribute` gives one expression's SQL, and \
`describe_table` says what a table holds and which Terms it carries. Prefer a reviewed SQL \
attribute over inventing the arithmetic yourself.
3. `check_answerable` is a cheap pre-check of whether the semantic layer covers a question's \
entities, before you pay for `ask_question`.

`check_readiness` answers a different question: whether Ontological can answer anything at all. \
Call it before a first question, and whenever an answer comes back empty or a tool says there is \
no source or no store: an empty answer is more often a database that isn't compiled than a \
question that was misunderstood.

Work through the semantic layer, not around it: there is deliberately no tool for browsing raw \
schemas. Every tool reads; nothing here changes the ontology, the catalog or the data.
"""


def build_server(engine: Engine, *, name: str = "ontological", version: str = "") -> MCPServer:
    """The MCP server with the eleven tools, each calling `engine`."""
    server = MCPServer(name=name, version=version, instructions=INSTRUCTIONS)
    tools = Tools(engine)

    @server.tool(annotations=READ_ONLY)
    async def check_readiness() -> dict[str, Any]:
        """Report whether Ontological can answer questions at all: whether a source is added and
        its catalog read, whether a database's semantic layer is compiled, and whether Claude Code
        is installed and logged in, which ask_question and check_answerable need.

        Use it before the first question, and whenever ask_question returns an empty answer. It
        is a few cheap reads, and it separates 'Ontological isn't set up' from 'the question
        wasn't understood', which otherwise look the same and are fixed in different places.
        Each database is listed with its tables and Terms; only a database with Terms can answer.
        """
        return await tools.check_readiness()

    @server.tool(annotations=READ_ONLY)
    async def search_terms(
        query: Annotated[str, Field(description="A business word or phrase, such as 'churn'.")],
        database: Annotated[
            str | None, Field(description="Search one database only. Omit to search all.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="How many Terms to return.")] = 10,
    ) -> dict[str, Any]:
        """Search the business glossary. Terms are the human-meaningful concepts Ontological has
        compiled over the person's databases ('Customer', 'Open Order', 'Net Revenue'), each
        mapped to real columns and SQL expressions. Use this first to learn what a business word
        means here before asking a question that depends on it. Nearest first."""
        return await tools.get("/terms", {"q": query, "database": database, "limit": limit})

    @server.tool(annotations=READ_ONLY)
    async def get_term(
        term_id: Annotated[str, Field(description="A Term's id, from search_terms.")],
    ) -> dict[str, Any]:
        """Fetch one glossary Term: its description, synonyms, certification, the tables it
        represents, and the Terms its tables join to. Use after search_terms to confirm a Term
        means what you assumed."""
        return _term_summary(await tools.term(term_id))

    @server.tool(annotations=READ_ONLY)
    async def get_term_columns(
        term_id: Annotated[str, Field(description="A Term's id, from search_terms.")],
    ) -> dict[str, Any]:
        """List the physical table columns a glossary Term maps to, each with its meaning and
        the Term its foreign key leads to. This is how you get from a business concept to the
        data behind it."""
        term = await tools.term(term_id)
        return {"term": _ref(term), "columns": term["attributes"], "joins": term["joins"]}

    @server.tool(annotations=READ_ONLY)
    async def get_term_sql_attributes(
        term_id: Annotated[str, Field(description="A Term's id, from search_terms.")],
    ) -> dict[str, Any]:
        """List the SQL attributes defined under a glossary Term: reviewed SQL expressions such
        as a margin, a ratio or a rolling total. Prefer reusing one of these over inventing the
        arithmetic yourself."""
        term = await tools.term(term_id)
        return {"term": _ref(term), "sql_attributes": term["sql_attributes"]}

    @server.tool(annotations=READ_ONLY)
    async def get_sql_attribute(
        attr_id: Annotated[str, Field(description="A SQL attribute's id.")],
    ) -> dict[str, Any]:
        """Fetch one SQL attribute: its expression, the SQL it was derived from, the Term it
        belongs to, the tables it reads, and what it is for."""
        return await tools.get(f"/sql-attributes/{attr_id}")

    @server.tool(annotations=READ_ONLY)
    async def describe_table(
        table_id: Annotated[str, Field(description="A table's id, from get_term's tables.")],
    ) -> dict[str, Any]:
        """Describe one table in full: its columns with their types, meanings, sample values and
        keys, the glossary Terms that represent it, and the SQL attributes whose SQL reads it.
        The richest single view of what a table is for."""
        table = await tools.get(f"/tables/{table_id}")
        semantics = await tools.get(f"/tables/{table_id}/semantics")
        return {**table, "terms": semantics["terms"], "sql_attributes": semantics["sql_attributes"]}

    @server.tool(annotations=READ_ONLY)
    async def check_answerable(
        question: Annotated[str, Field(description="The question, in plain language.")],
        database: Annotated[
            str | None,
            Field(description="Check one database. Omit for the one that covers it best."),
        ] = None,
    ) -> dict[str, Any]:
        """Grade how well the semantic layer covers the entities in a question, without running
        the agent: the coverage, each entity with the columns that cover it, and what is missing.
        A cheap pre-flight (one small Claude call): use it to decide whether Ontological can
        answer something before paying for ask_question, which is far slower."""
        body = {"question": question, "database": database}
        return await tools.post("/answerable", body, timeout=CHECK_TIMEOUT_S)

    @server.tool(annotations=READ_ONLY)
    async def ask_question(
        question: Annotated[str, Field(description="The question, in plain language.")],
        database: Annotated[
            str | None,
            Field(description="Answer from one database. Omit for the one that covers it best."),
        ] = None,
    ) -> dict[str, Any]:
        """Ask a natural-language question about the person's data. Ontological's agent plans
        it against the semantic layer, writes SQL, passes it through the validation gate, runs
        it, has a critic review it, and returns the answer with the SQL, the rows, and whether
        every step was verified. An unverified answer says why.

        This is slow (a minute or more) and uses Claude quota, so use check_answerable first
        when you are unsure the question is in scope, and prefer one well-formed question over
        several narrow ones. If you can write the SQL yourself, validate_sql and
        run_validated_sql cost no quota."""
        return await tools.ask(question, database)

    @server.tool(annotations=READ_ONLY)
    async def validate_sql(
        sql: Annotated[str, Field(description="One SELECT or WITH query.")],
        database: Annotated[
            str | None,
            Field(description="The database it runs on. Omit when there is only one."),
        ] = None,
    ) -> dict[str, Any]:
        """Check SQL before it runs, with Ontological's validation gate: exactly one read-only
        query, every table and column in the catalog, nothing degenerate (such as WHERE 1=0),
        aggregation on the many side of a join, joins the ontology's foreign keys bear out, and
        filter values and JSON keys the source holds. Returns ok with a validation_id to pass to
        run_validated_sql, or the issues to fix. Write the SQL in DuckDB's dialect."""
        return await tools.validate(sql, database)

    @server.tool(annotations=READ_ONLY)
    async def run_validated_sql(
        validation_id: Annotated[str, Field(description="The id validate_sql returned.")],
    ) -> dict[str, Any]:
        """Run SQL that passed validate_sql, by its validation_id, read-only. Returns the
        columns and rows (at most 200). When the query returns no rows, the outcome is 'empty'
        and the result says what to recheck: literal fixes found in the source, and the rows the
        query returns without its LIKE filters if those emptied it."""
        body = {"validation_id": validation_id}
        return await tools.post("/sql/run", body, timeout=RUN_TIMEOUT_S)

    return server


class Tools:
    """The requests behind the tools, and `validate_sql`'s count of failures in a row."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._failures: dict[str, int] = {}  # by database: SQL that failed the gate in a row

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = READ_TIMEOUT_S,
    ) -> Any:
        """The engine's JSON answer. Raises ToolError, which the caller reads, when there is no
        engine or it refuses the request."""
        if params is not None:
            params = {k: v for k, v in params.items() if v is not None}  # omitted, not "None"
        try:
            r = await self.engine.call(method, path, body=body, params=params, timeout=timeout)
        except EngineUnavailable as e:
            raise ToolError(f"Ontological's engine isn't available: {e}") from e
        if r.status_code >= 400:
            raise ToolError(_refusal(r))
        return r.json()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(
        self, path: str, body: dict[str, Any], *, timeout: float = READ_TIMEOUT_S
    ) -> Any:
        return await self.request("POST", path, body=body, timeout=timeout)

    async def term(self, term_id: str) -> dict[str, Any]:
        return await self.get(f"/terms/{term_id}")

    async def check_readiness(self) -> dict[str, Any]:
        blockers: list[str] = []
        notes: list[str] = []
        claude = await self._claude()
        if claude["state"] != "ready":
            blockers.append(
                f"Claude Code is {claude['state'].replace('_', ' ')}, and ask_question and "
                "check_answerable need it. Set it up in Ontological's onboarding, or run "
                "`claude` and log in. search_terms, validate_sql and run_validated_sql work "
                "without it."
            )
        databases: list[dict[str, Any]] = []
        for source in await self.get("/sources"):
            glossary = await self._glossary(source["id"])
            databases.append(
                {
                    "database": source["name"],
                    "source_id": source["id"],
                    "kind": source["kind"],
                    "tables": source["tables"],
                    "terms": glossary["total"],
                    "certified_terms": glossary["certified"],
                }
            )
        compiled = [d for d in databases if d["terms"]]
        if not databases:
            blockers.append(
                "No source is added, so there is nothing to ask about. Add a database or files "
                "in the Ontological app."
            )
        elif not compiled:
            blockers.append(
                "No database's semantic layer is compiled, so there are no Terms to resolve a "
                "question against. Compile one on the Ontological app's Jobs screen; it uses "
                "Claude quota."
            )
        for d in databases:
            if not d["tables"]:
                notes.append(
                    f"{d['database']} has no tables in its catalog: refresh the source in the app."
                )
            elif not d["terms"] and compiled:
                notes.append(
                    f"{d['database']} isn't compiled, so questions about it can't be answered "
                    "yet; validate_sql and run_validated_sql still work on it."
                )
        return {
            "ready": not blockers,
            "claude": claude,
            "databases": databases,
            "blockers": blockers,
            "notes": notes,
        }

    async def _claude(self) -> dict[str, Any]:
        try:
            got = await self.get("/claude")
        except ToolError as e:
            return {"state": "unknown", "logged_in": None, "error": str(e)}
        return {"state": got["state"], "logged_in": got["logged_in"]}

    async def _glossary(self, source_id: str) -> dict[str, Any]:
        """The source's Term counts; none when it has no glossary yet."""
        r = await self._call("GET", f"/sources/{source_id}/terms")
        if r.status_code == 404:
            return {"total": 0, "certified": 0}
        if r.status_code >= 400:
            raise ToolError(_refusal(r))
        return r.json()

    async def _call(self, method: str, path: str) -> httpx.Response:
        try:
            return await self.engine.call(method, path, timeout=READ_TIMEOUT_S)
        except EngineUnavailable as e:
            raise ToolError(f"Ontological's engine isn't available: {e}") from e

    async def ask(self, question: str, database: str | None) -> dict[str, Any]:
        asked = await self.post(
            "/ask", {"question": question, "database": database}, timeout=ASK_TIMEOUT_S
        )
        answer: dict[str, Any] = asked.get("answer") or {}
        result: dict[str, Any] = asked.get("result") or {}
        return {
            "answer": answer.get("answer"),
            "verified": asked["verified"],
            "unverified_because": asked.get("reason"),
            "database": asked["database"],
            "sql": asked.get("sql"),
            "columns": result.get("columns", []),
            "rows": result.get("rows", []),
            "truncated": result.get("truncated", False),
            "answerable": answer.get("answerable"),
            "missing": answer.get("missing", []),
            "assumptions": answer.get("assumptions", []),
            "terms_used": answer.get("terms_used", []),
            "steps": [_step(s) for s in asked.get("steps", [])],
            "restarted": asked.get("restarted"),
            "error": asked.get("error"),
        }

    async def validate(self, sql: str, database: str | None) -> dict[str, Any]:
        database = database or await self._only_database()
        failures = self._failures.get(database, 0)
        body = {
            "sql": sql,
            "database": database,
            "soft_as_warnings": failures >= SOFT_CHECKS_UNTIL,
        }
        validated = await self.post("/sql/validate", body)
        self._failures[database] = 0 if validated["ok"] else failures + 1
        return validated

    async def _only_database(self) -> str:
        names = [s["name"] for s in await self.get("/sources")]
        if len(names) == 1:
            return names[0]
        if not names:
            raise ToolError("No source is added. Call check_readiness to see what to set up.")
        raise ToolError(f"Name the database: one of {', '.join(names)}.")


def _refusal(r: httpx.Response) -> str:
    try:
        detail = r.json().get("detail")
    except ValueError:
        detail = None
    if not isinstance(detail, str):
        detail = r.text.strip()[:300] or r.reason_phrase
    if r.status_code == 503:
        return f"Ontological can't do that right now: {detail}. Call check_readiness."
    return f"Ontological refused ({r.status_code}): {detail}"


def _ref(term: dict[str, Any]) -> dict[str, Any]:
    return {"id": term["id"], "name": term["name"]}


def _term_summary(term: dict[str, Any]) -> dict[str, Any]:
    """`GET /terms/{id}` without its columns, SQL attributes and edit history, which have tools
    of their own or none."""
    keep = (
        "id",
        "name",
        "kind",
        "description",
        "synonyms",
        "name_certified",
        "description_certified",
        "tables",
    )
    return {
        **{k: term[k] for k in keep},
        "related_terms": _related(term["joins"]),
        "columns": len(term["attributes"]),
        "sql_attributes": len(term["sql_attributes"]),
    }


def _related(joins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for join in joins:
        seen.setdefault(join["to"]["id"], join["to"])
    return list(seen.values())


def _step(step: dict[str, Any]) -> str:
    """One line per tool call the agent made."""
    line = step["name"]
    if step.get("is_error"):
        line += ": failed"
    if step.get("session", 1) > 1:
        line += " (restart)"
    return line
