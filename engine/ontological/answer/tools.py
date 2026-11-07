"""The answer agent's tools (ARCHITECTURE.md §8.1): `plan`, `search_ontology`, `get_term`,
`describe_table`, `join_path`, `column_values`, `validate_sql`, `run_sql` and `review_answer`, for
one question's session.

Each tool checks what the session has already done and refuses, with a message that names the
missing step, when a step it needs hasn't happened:

| Tool | Needs |
|---|---|
| `plan` | nothing; the database it names, if any, must be the session's (see below) |
| `search_ontology`, `get_term`, `describe_table` | a plan |
| `join_path`, `column_values` | a plan |
| `validate_sql` | a plan that isn't a prediction; at most `MAX_VALIDATIONS` calls |
| `run_sql` | a calculation plan; a `validation_id` that passed `validate_sql` |
| `review_answer` | the critic on, a calculation plan, a `validation_id` that ran; at most |
| | `MAX_REVIEWS` calls |

For a question with no scope, `plan` may name any database the grounding pack's coverage ranked,
and later tools work against the latest plan's.

The latest plan decides: the model can plan again, and the post-run check reads the intent of the
plan the answer names.

`plan` records what the question asks and the database it is answered from. For a `calculation`
or `information` plan it runs upstream's per-entity retrieval for the plan's entities, evidence
and subject in that database (`Ontology.plan_pack`), and returns the candidates, the join paths
from the anchor and, for a calculation, its subtype's SQL template. A `prediction` plan is told
that predictions aren't supported, and how to end the session. `information` questions get no
`run_sql` and no `review_answer`: they are answered from the ontology.

`validate_sql` runs the whole gate (`ontological.answer.gate`), probing the source through one
`ProbeExecutor` per database for the session, so the question's probes share upstream's cap, and
`column_values` probes through the same one. After `SOFT_CHECKS_UNTIL` calls, the gate's soft
checks warn instead of failing the SQL.

`run_sql`'s result has an `outcome`: `rows`, or `empty` when the SQL returned none. An empty
result isn't an error, since it can be the answer, but the model is told to recheck its filters
and literals first, with the fixes the literal check finds once the query has come back empty,
and, when the SQL filters with LIKE or ILIKE, what the query returns without those filters
(`ontological.answer.like_check`). Those probes count against the same cap as the gate's.

A result's `data` is the tool's record of what it did, for the post-run check
(`ontological.answer.postcheck`); the gateway keeps it from the model, which reads the text.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Protocol

from claude_agent_sdk import SdkMcpTool
from sqlglot import exp

from ontological.answer import like_check, literal_check
from ontological.answer.gate import DIALECT, GateResult, KeyColumns, Probing, check_sql
from ontological.answer.join_check import JoinGraph
from ontological.answer.models import CALC_SUBTYPES, SUBTYPE_TEMPLATES, Review
from ontological.answer.pack import GroundingPack
from ontological.answer.probe import MAX_PROBE_ROWS, ProbeExecutor
from ontological.answer.render import render_hops, render_pack
from ontological.catalog.parsers.catalog_index import CatalogIndex
from ontological.sources.executor import Rows, SourceError

MAX_VALIDATIONS = 7  # upstream's cap on SQL attempts per question
SOFT_CHECKS_UNTIL = 5  # upstream's INTENT_VALIDATION_SKIPPED_AFTER: then soft checks warn
MAX_REVIEWS = 2  # §8.1: at most 2 critic rounds
REVIEW_ROWS = 20  # the first rows the critic sees
SEARCH_KINDS = ("attribute", "term")
COLUMN_VALUES = 20  # the values column_values returns unless asked for fewer or more
PREDICTION_UNSUPPORTED = "prediction_unsupported"  # what a prediction answer is missing

ToolResult = dict[str, Any]


class Ontology(Protocol):
    """What the tools read from the store about the session's database."""

    def catalog(self, database: str) -> CatalogIndex: ...

    def key_columns(self, database: str, table: str) -> KeyColumns: ...

    def join_graph(self, database: str) -> JoinGraph: ...

    def search(self, database: str, query: str, kinds: Sequence[str]) -> list[dict[str, Any]]: ...

    def describe_table(self, database: str, table: str) -> dict[str, Any] | None: ...

    def get_term(self, database: str, term: str) -> dict[str, Any] | None: ...

    def join_path(
        self, database: str, from_table: str, to_table: str
    ) -> list[dict[str, str]] | None: ...

    def plan_pack(
        self,
        database: str,
        question: str,
        *,
        entities: Sequence[str],
        evidence: str,
        subject: str,
        anchor_attribute_id: str | None,
    ) -> GroundingPack: ...


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
Runners = Callable[[str], SqlRunner]  # a database's source


@dataclass
class _Validation:
    validation_id: str
    plan_id: str
    database: str
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
    """The tools of one question's session, over `database`, or over one of `others` that a
    plan names when the question has no scope."""

    def __init__(
        self,
        question: str,
        *,
        database: str,
        ontology: Ontology,
        runners: Runners,
        review: Reviewer | None,
        others: Sequence[str] = (),
        shown_tables: Collection[str] = (),
    ) -> None:
        """`database` is the scope, or else the one the grounding pack's coverage chose, and
        `others` the rest of that ranking. `runners` opens a database's source. `review` is the
        critic; None turns it off, and `review_answer` isn't offered. `shown_tables` are the ids
        of the tables the system prompt's pack shows, which `plan` names without listing them
        again."""
        self.question = question
        self.database = database
        self.others = [d for d in others if d != database]
        self.ontology = ontology
        self.runners = runners
        self.review = review
        self._state = _State()
        self._catalogs: dict[str, CatalogIndex] = {}
        self._probings: dict[str, Probing] = {}
        self._shown: dict[str, set[str]] = {database: set(shown_tables)}

    def sdk_tools(self) -> list[SdkMcpTool[Any]]:
        tools = [
            SdkMcpTool("plan", PLAN_DOC, PLAN_SCHEMA, self.plan),
            SdkMcpTool("search_ontology", SEARCH_DOC, SEARCH_SCHEMA, self.search_ontology),
            SdkMcpTool("get_term", TERM_DOC, TERM_SCHEMA, self.get_term),
            SdkMcpTool("describe_table", DESCRIBE_DOC, DESCRIBE_SCHEMA, self.describe_table),
            SdkMcpTool("join_path", JOIN_DOC, JOIN_SCHEMA, self.join_path),
            SdkMcpTool("column_values", VALUES_DOC, VALUES_SCHEMA, self.column_values),
            SdkMcpTool("validate_sql", VALIDATE_DOC, VALIDATE_SCHEMA, self.validate_sql),
            SdkMcpTool("run_sql", RUN_DOC, RUN_SCHEMA, self.run_sql),
        ]
        if self.review is not None:
            tools.append(SdkMcpTool("review_answer", REVIEW_DOC, REVIEW_SCHEMA, self.review_answer))
        return tools

    # --- the tools ------------------------------------------------------------------------------

    async def plan(self, args: dict[str, Any]) -> ToolResult:
        state = self._state
        named = args.get("database") or None
        database = named or (state.plans[state.plan_id]["database"] if state.plan_id else None)
        database = database or self.database
        if database not in (self.database, *self.others):
            if not self.others:
                return _refuse(
                    f"this question is answered from {self.database}, not {named}. "
                    f"Call plan again with database={self.database!r}, or leave it out."
                )
            return _refuse(
                f"{named} isn't a database retrieval found this question in. Call plan again "
                f"with one of {', '.join([self.database, *self.others])}, or leave it out for "
                f"{self.database}."
            )
        plan_id = f"plan-{len(state.plans) + 1}"
        intent = str(args["intent"])
        plan: dict[str, Any] = {
            "plan_id": plan_id,
            "database": database,
            "intent": intent,
            "calc_subtype": args.get("calc_subtype"),
            "entities": list(args.get("entities") or []),
            "evidence": list(args.get("evidence") or []),
            "subject": args.get("subject") or "",
            "anchor_attribute_id": args.get("anchor_attribute_id"),
        }
        state.plans[plan_id] = plan
        state.plan_id = plan_id
        if intent == "prediction":
            return {
                "content": [_text(_json(plan)), _text(_PREDICTION)],
                "data": {**plan, "supported": False},
            }

        pack = await asyncio.to_thread(
            self.ontology.plan_pack,
            database,
            self.question,
            entities=plan["entities"],
            evidence="\n".join(plan["evidence"]),
            subject=plan["subject"],
            anchor_attribute_id=plan["anchor_attribute_id"],
        )
        shown = self._shown.setdefault(database, set())
        content = [
            _text(_json(plan)),
            _text(
                "## Candidates for the plan's entities\n" + render_pack(pack, shown_tables=shown)
            ),
        ]
        shown.update(str(t["id"]) for t in pack.tables)
        subtype = plan["calc_subtype"]
        if intent == "calculation" and subtype in SUBTYPE_TEMPLATES:
            content.append(
                _text(
                    f"## The {subtype} template\nThe shape of a {subtype} query, from another "
                    f"database: its tables and values aren't this one's.\n```sql\n"
                    f"{SUBTYPE_TEMPLATES[subtype]}\n```"
                )
            )
        if intent == "information":
            content.append(_text(_INFORMATION))
        return {
            "content": content,
            "data": {
                **plan,
                "anchor": pack.anchor["id"] if pack.anchor else None,
                "attribute_ids": [str(a["id"]) for a in pack.attributes],
                "sql_attribute_ids": [str(a["id"]) for a in pack.sql_attributes],
                "tables": [str(t["name"]) for t in pack.tables],
            },
        }

    async def search_ontology(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan():
            return refused
        kinds = list(args.get("kinds") or SEARCH_KINDS)
        hits = await asyncio.to_thread(self.ontology.search, self._database(), args["query"], kinds)
        return _ok({"hits": hits})

    async def get_term(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan():
            return refused
        database = self._database()
        term = await asyncio.to_thread(self.ontology.get_term, database, args["id"])
        if term is None:
            return _failed(
                f"There is no Term {args['id']!r} in {database}.", {"ok": False, "id": args["id"]}
            )
        return _ok({"term": term})

    async def describe_table(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan():
            return refused
        database = self._database()
        table = await asyncio.to_thread(self.ontology.describe_table, database, args["id"])
        if table is None:
            return _failed(
                f"There is no table {args['id']!r} in {database}.",
                {"ok": False, "id": args["id"]},
            )
        return _ok({"table": table})

    async def join_path(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan():
            return refused
        database = self._database()
        start, end = str(args["from_table"]), str(args["to_table"])
        hops = await asyncio.to_thread(self.ontology.join_path, database, start, end)
        if hops is None:
            return _failed(
                f"{start!r} or {end!r} isn't a table in {database}. Name tables as the "
                "grounding pack or describe_table does.",
                {"ok": False, "from_table": start, "to_table": end},
            )
        found = {"from_table": start, "to_table": end, "path": hops}
        if not hops:
            return {
                "content": [
                    _text(
                        f"The ontology has no join path from {start} to {end}: no semantic "
                        "foreign keys connect them, or they are the same table."
                    )
                ],
                "data": found,
            }
        return {"content": [_text(f"{start} to {end}: {render_hops(hops)}")], "data": found}

    async def column_values(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan():
            return refused
        database = self._database()
        named = str(args["column"])
        limit = max(1, min(int(args.get("limit") or COLUMN_VALUES), MAX_PROBE_ROWS))
        like = args.get("like") or None
        found = await asyncio.to_thread(self._column_values, database, named, like, limit)
        if "error" in found:
            return _failed(found["error"], {"ok": False, "column": named})
        return _ok(found)

    async def validate_sql(self, args: dict[str, Any]) -> ToolResult:
        if refused := self._needs_plan() or self._withheld("validate_sql", "prediction"):
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
        database = self._database()
        gate = await asyncio.to_thread(self._gate, database, sql, soft_as_warnings)
        if not gate.ok:
            return _failed(
                "validate_sql found issues. Fix the SQL and call validate_sql again:\n"
                + "\n".join(f"- {issue}" for issue in gate.issues),
                {"ok": False, "issues": gate.issues},
            )
        assert state.plan_id is not None
        validation_id = f"val-{len(state.validations) + 1}"
        state.validations[validation_id] = _Validation(
            validation_id, state.plan_id, database, sql, gate
        )
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
        if refused := self._needs_plan() or self._withheld("run_sql", "prediction", "information"):
            return refused
        validation_id = str(args["validation_id"])
        validation = self._state.validations.get(validation_id)
        if validation is None:
            return _refuse(
                f"{validation_id!r} isn't a validation_id validate_sql issued. Call validate_sql "
                "with the SQL, then run_sql with the validation_id it returns."
            )
        try:
            runner = self._runner(validation.database)
            rows = await asyncio.to_thread(runner.run_sql, validation.sql)
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
        ran["repairs"] = await asyncio.to_thread(self._repairs, validation.database, validation.sql)
        ran["without_like"] = await asyncio.to_thread(
            self._without_like, validation.database, validation.sql
        )
        return {"content": [_text(_empty_message(ran))], "data": ran}

    async def review_answer(self, args: dict[str, Any]) -> ToolResult:
        withheld = self._withheld("review_answer", "prediction", "information")
        if refused := self._needs_plan() or withheld:
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

    def _withheld(self, tool: str, *intents: str) -> ToolResult | None:
        """A refusal when the latest plan's intent is one of `intents`."""
        plan_id = self._state.plan_id
        intent = self._state.plans[plan_id]["intent"] if plan_id else None
        if intent not in intents:
            return None
        if intent == "prediction":
            return _refuse(
                f"{plan_id} is a prediction, which Ontological doesn't answer, so {tool} is "
                f"withheld. {_END_PREDICTION}"
            )
        return _refuse(
            f"{plan_id} is an information question, answered from the ontology with no SQL, so "
            f"{tool} is withheld. If the question needs SQL after all, call plan again with "
            "intent calculation."
        )

    def _database(self) -> str:
        """The latest plan's database."""
        plan_id = self._state.plan_id
        return self._state.plans[plan_id]["database"] if plan_id else self.database

    def _runner(self, database: str) -> SqlRunner:
        return self.runners(database)

    def _catalog(self, database: str) -> CatalogIndex:
        if database not in self._catalogs:
            self._catalogs[database] = self.ontology.catalog(database)
        return self._catalogs[database]

    def _gate(self, database: str, sql: str, soft_as_warnings: bool) -> GateResult:
        return check_sql(
            sql,
            self._catalog(database),
            key_columns=lambda table: self.ontology.key_columns(database, table),
            probing=self._session_probing(database),
            soft_as_warnings=soft_as_warnings,
        )

    def _session_probing(self, database: str) -> Probing:
        """The question's probes of `database`, shared by the gate, `column_values` and
        empty-result handling, so all of them count against one `MAX_PROBES`."""
        if database not in self._probings:
            self._probings[database] = Probing(
                ProbeExecutor(self._runner(database)), self.ontology.join_graph(database)
            )
        return self._probings[database]

    def _repairs(self, database: str, sql: str) -> str:
        """Fixes the literal check finds for SQL that returned no rows. The query is empty, so
        literals on columns with more than `LOW_CARD_THRESHOLD` values, which the gate leaves
        alone, are probed for case and whitespace variants too."""
        probes = self._session_probing(database).probes
        found = literal_check.find_literal_mismatches(probes, DIALECT, sql, result_was_empty=True)
        found += literal_check.find_numeric_scale_mismatches(probes, DIALECT, sql)
        return literal_check.build_value_repair_error(found) if found else ""

    def _column_values(
        self, database: str, named: str, like: str | None, limit: int
    ) -> dict[str, Any]:
        """Two probes of one column: its range, and its most frequent values (those `like`
        matches, ignoring case, when it is given). `{"error": ...}` when the column isn't in the
        catalog or a probe fails."""
        place = _resolve_column(self._catalog(database), named)
        if isinstance(place, str):
            return {"error": place}
        schema, table, column = place
        source = exp.table_(table, db=schema, quoted=True)
        col = exp.column(column, quoted=True)
        probes = self._session_probing(database).probes
        stats = probes.run(
            exp.select(
                exp.Min(this=col).as_("min"),
                exp.Max(this=col).as_("max"),
                exp.Count(this=exp.Distinct(expressions=[col])).as_("distinct_count"),
                exp.Sub(this=exp.Count(this=exp.Star()), expression=exp.Count(this=col)).as_(
                    "nulls"
                ),
            )
            .from_(source)
            .sql(dialect=DIALECT),
            "column_values: range",
        )
        keep = exp.Not(this=exp.Is(this=col, expression=exp.Null()))
        if like:
            matches = exp.ILike(
                this=exp.Cast(this=col, to=exp.DataType.build("VARCHAR")),
                expression=exp.Literal.string(like),
            )
            keep = exp.and_(keep, matches)
        values = probes.run(
            exp.select(col.as_("value"), exp.Count(this=exp.Star()).as_("rows"))
            .from_(source)
            .where(keep)
            .group_by(col)
            .order_by(exp.Ordered(this=exp.column("rows"), desc=True), exp.Ordered(this=col))
            .limit(limit)
            .sql(dialect=DIALECT),
            "column_values: values",
        )
        for probe in (stats, values):
            if not probe["ok"]:
                return {"error": f"Couldn't read {named}: {probe['error']}."}
        summaries: list[dict[str, Any]] = stats["rows"] or [{}]
        summary = summaries[0]
        rows: list[dict[str, Any]] = values["rows"] or []
        return {
            "column": f"{table}.{column}",
            "like": like,
            "values": [{"value": _cell(r["value"]), "rows": _cell(r["rows"])} for r in rows],
            "more": len(rows) >= limit,
            **{k: _cell(summary.get(k)) for k in ("min", "max", "distinct_count", "nulls")},
        }

    def _without_like(self, database: str, sql: str) -> dict[str, Any] | None:
        """What SQL that returned no rows returns without its LIKE and ILIKE filters, when that
        is some rows; otherwise None."""
        found = like_check.probe_without_like(self._session_probing(database).probes, DIALECT, sql)
        if found is None:
            return None
        return {
            "dropped": found.dropped,
            "sql": found.sql,
            "columns": found.columns,
            "rows": [[_cell(v) for v in row] for row in found.rows],
            "truncated": found.truncated,
        }


def _resolve_column(catalog: CatalogIndex, named: str) -> tuple[str, str, str] | str:
    """`(schema, table, column)` as the catalog spells the table and column, for
    `Table.Column` or `schema.Table.Column`; otherwise what is wrong, for the model."""
    parts = [p.strip().strip('"') for p in named.split(".")]
    if len(parts) not in (2, 3) or not all(parts):
        return f"Name the column as Table.Column or schema.Table.Column, not {named!r}."
    *schema_part, table_name, column_name = parts
    wanted_schema = schema_part[0].lower() if schema_part else None
    for schema, entry in catalog.schemas.items():
        if wanted_schema is not None and schema != wanted_schema:
            continue
        table = entry.tables.get(table_name.lower())
        if table is None:
            continue
        spelled = table.spellings.get(column_name.lower())
        if spelled is None:
            columns = ", ".join(sorted(table.spellings.values()))
            return f"{table.name} has no column {column_name!r}. Its columns: {columns}."
        return schema, table.name, spelled
    return f"There is no table {'.'.join(parts[:-1])!r} in this database."


def _ok(data: dict[str, Any]) -> ToolResult:
    return {"content": [_text(_json(data))], "data": data}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


_END_PREDICTION = (
    'End the session with answerable=false, missing=["prediction_unsupported"] and no SQL, '
    "and say in the answer that Ontological answers questions about the data as it is, not "
    "forecasts. If the question isn't a prediction after all, call plan again with its intent."
)
_PREDICTION = (
    "Predictions aren't supported: Ontological doesn't forecast or predict. " + _END_PREDICTION
)
_INFORMATION = (
    "This is an information question: answer it from the ontology, with what the pack and "
    "search_ontology, get_term, describe_table, join_path and column_values show, and give no "
    "SQL. run_sql and review_answer are withheld for it. If the question needs SQL after all, "
    "call plan again with intent calculation."
)


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
    found = [part for part in (ran["repairs"], _without_like_message(ran["without_like"])) if part]
    if found:
        return (
            f"{head}\n\n" + "\n\n".join(found) + "\n\nIf a fix above matches the question, "
            "make it, call validate_sql with the new SQL and run that. If none does, the empty "
            f"result can be the answer: answer with {validation_id}."
        )
    return (
        f"{head} Probing the filters' literals found no near-miss. If the filters are right, "
        f"the empty result can be the answer: answer with {validation_id}."
    )


def _without_like_message(without_like: dict[str, Any] | None) -> str:
    if without_like is None:
        return ""
    dropped = without_like["dropped"]
    filters = "this LIKE filter" if len(dropped) == 1 else "these LIKE filters"
    more = ", and more" if without_like["truncated"] else ""
    rows = "\n".join(json.dumps(row, ensure_ascii=False) for row in without_like["rows"])
    return (
        f"Without {filters}, the same query returns rows, so the LIKE filtering emptied the "
        "result:\n"
        + "\n".join(f"- {f}" for f in dropped)
        + f"\nIts first rows ({', '.join(without_like['columns'])}){more}:\n{rows}\n"
        "Check each pattern against how the source writes the value: its case, its spelling, "
        "its wildcards, or whether the value is in another column."
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
    "calculation, the entities and evidence in it, its subject (the business concept it is "
    "about), and the anchor attribute (an attribute id from the grounding pack) when one fits. "
    "Returns the plan_id the answer must carry, the ontology's candidates for each entity with "
    "the join paths from the anchor, and for a calculation an example of its subtype's SQL."
)
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["calculation", "information", "prediction"]},
        "calc_subtype": {"type": "string", "enum": sorted(CALC_SUBTYPES)},
        "entities": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "subject": {"type": "string"},
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

TERM_DOC = (
    "A Term (a business concept) in full: its description, synonyms, the tables it maps to, and "
    "its attributes and SQL attributes."
)
TERM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string", "description": "The Term's id or its name."}},
    "required": ["id"],
    "additionalProperties": False,
}

DESCRIBE_DOC = "A table's columns, with their types, descriptions and sample values."
DESCRIBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string", "description": "The table's id or its name."}},
    "required": ["id"],
    "additionalProperties": False,
}

JOIN_DOC = (
    "The shortest join path between two tables over the ontology's semantic foreign keys, hop "
    "by hop."
)
JOIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "from_table": {"type": "string", "description": "A table's name or id."},
        "to_table": {"type": "string", "description": "A table's name or id."},
    },
    "required": ["from_table", "to_table"],
    "additionalProperties": False,
}

VALUES_DOC = (
    "Read one column's values from the source: its min, max, distinct and null counts, and its "
    "most frequent values with their row counts, those matching `like` when it is given. Use it "
    "to find how a value the question names is stored. Each call counts against the question's "
    "probe budget."
)
VALUES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "column": {"type": "string", "description": "Table.Column or schema.Table.Column."},
        "like": {
            "type": "string",
            "description": "A LIKE pattern, matched ignoring case; % matches any run of text.",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PROBE_ROWS},
    },
    "required": ["column"],
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
    "when the SQL returns no rows, says so with any fixes its filters' literals need and "
    "whether its LIKE filters are what emptied it."
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
