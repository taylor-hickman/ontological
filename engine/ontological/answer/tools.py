"""The answer agent's tools (ARCHITECTURE.md §8.1): `plan`, `search_ontology`, `describe_table`,
`validate_sql`, `run_sql` and `review_answer`, for one question's session.

Each tool checks what the session has already done and refuses, with a message that names the
missing step, when a step it needs hasn't happened:

| Tool | Needs |
|---|---|
| `plan` | nothing; the database it names, if any, must be the session's |
| `search_ontology`, `describe_table` | a plan |
| `validate_sql` | a plan; at most `MAX_VALIDATIONS` calls |
| `run_sql` | a `validation_id` that passed `validate_sql` |
| `review_answer` | the critic on, a `validation_id` that ran; at most `MAX_REVIEWS` calls |

`validate_sql` runs the whole gate (`ontological.answer.gate`), probing the source through one
`ProbeExecutor` for the session, so the question's probes share upstream's cap. After
`SOFT_CHECKS_UNTIL` calls, the gate's soft checks warn instead of failing the SQL.

`run_sql`'s result has an `outcome`: `rows`, or `empty` when the SQL returned none. An empty
result isn't an error, since it can be the answer, but the model is told to recheck its filters
and literals first, with the fixes the literal check finds once the query has come back empty.
Those probes count against the same cap as the gate's.

A result's `data` is the tool's record of what it did, for the post-run check
(`ontological.answer.postcheck`); the gateway keeps it from the model, which reads the text.

Intents are recorded but don't yet change what the tools allow: withholding `run_sql` and
`review_answer` from `information` questions, and `plan`'s answer to `prediction`, are M4.3's.
So are per-entity retrieval in `plan` and the subtype's SQL template.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Protocol

from claude_agent_sdk import SdkMcpTool

from ontological.answer import literal_check
from ontological.answer.gate import DIALECT, GateResult, KeyColumns, Probing, check_sql
from ontological.answer.join_check import JoinGraph
from ontological.answer.models import CALC_SUBTYPES, Review
from ontological.answer.probe import ProbeExecutor
from ontological.catalog.parsers.catalog_index import CatalogIndex
from ontological.sources.executor import Rows, SourceError

MAX_VALIDATIONS = 7  # upstream's cap on SQL attempts per question
SOFT_CHECKS_UNTIL = 5  # upstream's INTENT_VALIDATION_SKIPPED_AFTER: then soft checks warn
MAX_REVIEWS = 2  # §8.1: at most 2 critic rounds
REVIEW_ROWS = 20  # the first rows the critic sees
SEARCH_KINDS = ("attribute", "term")

ToolResult = dict[str, Any]


class Ontology(Protocol):
    """What the tools read from the store about the session's database."""

    def catalog(self, database: str) -> CatalogIndex: ...

    def key_columns(self, database: str, table: str) -> KeyColumns: ...

    def join_graph(self, database: str) -> JoinGraph: ...

    def search(self, database: str, query: str, kinds: Sequence[str]) -> list[dict[str, Any]]: ...

    def describe_table(self, database: str, table: str) -> dict[str, Any] | None: ...


class SqlRunner(Protocol):
    """The source's executor: one SELECT, its rows capped (`SourceExecutor.run_sql`), and the
    gate's read-only probes (`SourceExecutor.probe`)."""

    def run_sql(self, sql: str) -> Rows: ...

    def probe(self, sql: str, *, max_rows: int) -> Rows: ...


@dataclass(frozen=True)
class ReviewInput:
    """What the critic is shown: the question, the plan, the SQL and what it returned."""

    question: str
    plan: dict[str, Any]
    sql: str
    tables: list[str]  # schema.Table, from the gate
    used_columns: list[str]  # Table.Column, from the gate
    columns: list[str]  # the result's
    rows: list[list[Any]]  # the first REVIEW_ROWS
    row_count: int
    truncated: bool
    answer_draft: str


Reviewer = Callable[[ReviewInput], Awaitable[Review | None]]


@dataclass
class _Validation:
    validation_id: str
    plan_id: str
    sql: str
    gate: GateResult


@dataclass
class _State:
    plans: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    plan_id: str | None = None  # the latest plan's
    validations: dict[str, _Validation] = field(default_factory=dict[str, _Validation])
    validate_calls: int = 0
    runs: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    reviews: int = 0


class AnswerTools:
    """The tools of one question's session, over `database`."""

    def __init__(
        self,
        question: str,
        *,
        database: str,
        ontology: Ontology,
        runner: SqlRunner,
        review: Reviewer | None,
    ) -> None:
        """`review` is the critic; None turns it off, and `review_answer` isn't offered."""
        self.question = question
        self.database = database
        self.ontology = ontology
        self.runner = runner
        self.review = review
        self._state = _State()
        self._catalog: CatalogIndex | None = None
        self._probing: Probing | None = None

    def sdk_tools(self) -> list[SdkMcpTool[Any]]:
        tools = [
            SdkMcpTool("plan", PLAN_DOC, PLAN_SCHEMA, self.plan),
            SdkMcpTool("search_ontology", SEARCH_DOC, SEARCH_SCHEMA, self.search_ontology),
            SdkMcpTool("describe_table", DESCRIBE_DOC, DESCRIBE_SCHEMA, self.describe_table),
            SdkMcpTool("validate_sql", VALIDATE_DOC, VALIDATE_SCHEMA, self.validate_sql),
            SdkMcpTool("run_sql", RUN_DOC, RUN_SCHEMA, self.run_sql),
        ]
        if self.review is not None:
            tools.append(SdkMcpTool("review_answer", REVIEW_DOC, REVIEW_SCHEMA, self.review_answer))
        return tools

    # --- the tools ------------------------------------------------------------------------------

    async def plan(self, args: dict[str, Any]) -> ToolResult:
        named = args.get("database")
        if named and named != self.database:
            return _refuse(
                f"this question is answered from {self.database}, not {named}. "
                f"Call plan again with database={self.database!r}, or leave it out."
            )
        state = self._state
        plan_id = f"plan-{len(state.plans) + 1}"
        plan = {
            "plan_id": plan_id,
            "database": self.database,
            "intent": args["intent"],
            "calc_subtype": args.get("calc_subtype"),
            "entities": list(args.get("entities") or []),
            "evidence": list(args.get("evidence") or []),
            "anchor_attribute_id": args.get("anchor_attribute_id"),
        }
        state.plans[plan_id] = plan
        state.plan_id = plan_id
        return _ok(plan)

    async def search_ontology(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan():
            return refused
        kinds = list(args.get("kinds") or SEARCH_KINDS)
        hits = await asyncio.to_thread(self.ontology.search, self.database, args["query"], kinds)
        return _ok({"hits": hits})

    async def describe_table(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan():
            return refused
        table = await asyncio.to_thread(self.ontology.describe_table, self.database, args["id"])
        if table is None:
            return _failed(
                f"There is no table {args['id']!r} in {self.database}.",
                {"ok": False, "id": args["id"]},
            )
        return _ok({"table": table})

    async def validate_sql(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan():
            return refused
        state = self._state
        if state.validate_calls >= MAX_VALIDATIONS:
            return _refuse(
                f"validate_sql has been called {MAX_VALIDATIONS} times, its limit for one "
                "question. End the session with answerable=false, and say in missing what kept "
                "the SQL from validating."
            )
        state.validate_calls += 1
        sql = str(args["sql"])
        soft_as_warnings = state.validate_calls > SOFT_CHECKS_UNTIL
        gate = await asyncio.to_thread(self._gate, sql, soft_as_warnings)
        if not gate.ok:
            return _failed(
                "validate_sql found issues. Fix the SQL and call validate_sql again:\n"
                + "\n".join(f"- {issue}" for issue in gate.issues),
                {"ok": False, "issues": gate.issues},
            )
        assert state.plan_id is not None
        validation_id = f"val-{len(state.validations) + 1}"
        state.validations[validation_id] = _Validation(validation_id, state.plan_id, sql, gate)
        passed = _ok(
            {
                "ok": True,
                "validation_id": validation_id,
                "plan_id": state.plan_id,
                "sql": sql,
                "tables": gate.tables,
                "columns": gate.columns,
                "warnings": gate.warnings,
            }
        )
        if gate.warnings:
            passed["content"].append(
                _text(
                    "Warnings: the SQL passed, since the soft checks no longer block it after "
                    f"{SOFT_CHECKS_UNTIL} tries, but they still found:\n"
                    + "\n".join(f"- {warning}" for warning in gate.warnings)
                )
            )
        return passed

    async def run_sql(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan():
            return refused
        validation_id = str(args["validation_id"])
        validation = self._state.validations.get(validation_id)
        if validation is None:
            return _refuse(
                f"{validation_id!r} isn't a validation_id validate_sql issued. Call validate_sql "
                "with the SQL, then run_sql with the validation_id it returns."
            )
        try:
            rows = await asyncio.to_thread(self.runner.run_sql, validation.sql)
        except SourceError as e:
            return _failed(
                f"The source couldn't run the SQL: {e}. Fix the SQL, then validate it again.",
                {"ok": False, "validation_id": validation_id, "error": str(e)},
            )
        ran: dict[str, Any] = {
            "validation_id": validation_id,
            "outcome": "rows" if rows.rows else "empty",
            "columns": rows.columns,
            "types": rows.types,
            "rows": [[_cell(v) for v in row] for row in rows.rows],
            "row_count": len(rows.rows),
            "truncated": rows.truncated,
        }
        self._state.runs[validation_id] = ran
        if rows.rows:
            return _ok(ran)
        ran["repairs"] = await asyncio.to_thread(self._repairs, validation.sql)
        return {"content": [_text(_empty_message(ran))], "data": ran}

    async def review_answer(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan():
            return refused
        if self.review is None:
            return _refuse("the critic is off for this session, so no review is needed.")
        state = self._state
        validation_id = str(args["validation_id"])
        ran = state.runs.get(validation_id)
        validation = state.validations.get(validation_id)
        if ran is None or validation is None:
            return _refuse(
                f"{validation_id!r} hasn't run. Call run_sql with a validation_id from "
                "validate_sql first, then review that validation_id."
            )
        if state.reviews >= MAX_REVIEWS:
            return _refuse(
                f"review_answer has run {MAX_REVIEWS} times, its limit for one question. "
                "Finish with the answer you have."
            )
        state.reviews += 1
        verdict = await self.review(
            ReviewInput(
                question=self.question,
                plan=state.plans[validation.plan_id],
                sql=validation.sql,
                tables=validation.gate.tables,
                used_columns=validation.gate.columns,
                columns=ran["columns"],
                rows=ran["rows"][:REVIEW_ROWS],
                row_count=ran["row_count"],
                truncated=ran["truncated"],
                answer_draft=str(args.get("answer_draft", "")),
            )
        )
        review = {
            "review_id": f"rev-{state.reviews}",
            "validation_id": validation_id,
            # A critic that doesn't answer passes the SQL, as upstream's intent validation does
            "passed": True if verdict is None else verdict.passed,
            "reviewed": verdict is not None,
            "issues": [] if verdict is None else verdict.issues,
            "reasoning": "" if verdict is None else verdict.reasoning,
        }
        return _ok(review)

    # --- helpers --------------------------------------------------------------------------------

    def _needs_plan(self) -> ToolResult | None:
        if self._state.plan_id is None:
            return _refuse(
                "call plan first. Every other tool works against the plan, and the answer "
                "needs the plan_id it returns."
            )
        return None

    def _gate(self, sql: str, soft_as_warnings: bool) -> GateResult:
        if self._catalog is None:
            self._catalog = self.ontology.catalog(self.database)
        return check_sql(
            sql,
            self._catalog,
            key_columns=lambda table: self.ontology.key_columns(self.database, table),
            probing=self._session_probing(),
            soft_as_warnings=soft_as_warnings,
        )

    def _session_probing(self) -> Probing:
        """The question's probes, shared by the gate and empty-result handling, so all of them
        count against one `MAX_PROBES`."""
        if self._probing is None:
            self._probing = Probing(
                ProbeExecutor(self.runner), self.ontology.join_graph(self.database)
            )
        return self._probing

    def _repairs(self, sql: str) -> str:
        """Fixes the literal check finds for SQL that returned no rows. The query is empty, so
        literals on columns with more than `LOW_CARD_THRESHOLD` values, which the gate leaves
        alone, are probed for case and whitespace variants too."""
        probes = self._session_probing().probes
        found = literal_check.find_literal_mismatches(probes, DIALECT, sql, result_was_empty=True)
        found += literal_check.find_numeric_scale_mismatches(probes, DIALECT, sql)
        return literal_check.build_value_repair_error(found) if found else ""


def _ok(data: dict[str, Any]) -> ToolResult:
    return {"content": [_text(json.dumps(data, ensure_ascii=False, default=str))], "data": data}


def _failed(message: str, data: dict[str, Any]) -> ToolResult:
    """The step ran and failed, such as SQL with issues: the model fixes it and tries again."""
    return {"content": [_text(message)], "is_error": True, "data": data}


def _refuse(message: str) -> ToolResult:
    """A step this one needs hasn't happened."""
    return {"content": [_text(f"Refused: {message}")], "is_error": True, "data": {}}


def _empty_message(ran: dict[str, Any]) -> str:
    validation_id = ran["validation_id"]
    head = (
        f"run_sql ran {validation_id} and it returned no rows (columns: "
        f"{', '.join(ran['columns'])}). An empty result can be the answer, but more often a "
        "filter or a literal is wrong. Before you answer, recheck each filter and literal "
        "against the question and the values the source holds."
    )
    if ran["repairs"]:
        return (
            f"{head}\n\n{ran['repairs']}\n\nIf a fix above matches the question, make it, call "
            "validate_sql with the new SQL and run that. If none does, the empty result can be "
            f"the answer: answer with {validation_id}."
        )
    return (
        f"{head} Probing the filters' literals found no near-miss. If the filters are right, "
        f"the empty result can be the answer: answer with {validation_id}."
    )


def _text(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _cell(value: Any) -> Any:
    """A value as JSON holds it."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


# --- what the model reads about each tool ---------------------------------------------------

PLAN_DOC = (
    "Call this first. Record what the question asks: its intent, the calculation subtype for a "
    "calculation, the entities and evidence in it, and the anchor attribute (an attribute id "
    "from the grounding pack) when one fits. Returns the plan_id the answer must carry."
)
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["calculation", "information", "prediction"]},
        "calc_subtype": {"type": "string", "enum": sorted(CALC_SUBTYPES)},
        "entities": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "anchor_attribute_id": {"type": "string"},
        "database": {"type": "string"},
    },
    "required": ["intent", "entities"],
    "additionalProperties": False,
}

SEARCH_DOC = (
    "Search the ontology for more candidates than the grounding pack holds: attributes "
    "(columns with their meaning) and Terms (business concepts)."
)
SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "kinds": {"type": "array", "items": {"type": "string", "enum": list(SEARCH_KINDS)}},
    },
    "required": ["query"],
    "additionalProperties": False,
}

DESCRIBE_DOC = "A table's columns, with their types, descriptions and sample values."
DESCRIBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string", "description": "The table's id or its name."}},
    "required": ["id"],
    "additionalProperties": False,
}

VALIDATE_DOC = (
    "Check SQL before it runs: one read-only query, every table and column in the catalog, "
    "nothing degenerate, joins the ontology bears out, and filter values and JSON keys the "
    "source holds. Returns a validation_id, or the issues to fix."
)
VALIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"sql": {"type": "string"}},
    "required": ["sql"],
    "additionalProperties": False,
}

RUN_DOC = (
    "Run SQL that passed validate_sql, by its validation_id. Returns the columns and rows, or, "
    "when the SQL returns no rows, says so with any fixes its filters' literals need."
)
RUN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"validation_id": {"type": "string"}},
    "required": ["validation_id"],
    "additionalProperties": False,
}

REVIEW_DOC = (
    "Ask a critic whether SQL that ran answers the question. Pass its validation_id and your "
    "draft answer. Returns a review_id, and whether it passed or the issues to fix."
)
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"validation_id": {"type": "string"}, "answer_draft": {"type": "string"}},
    "required": ["validation_id", "answer_draft"],
    "additionalProperties": False,
}
