# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: the SQL rules (`SQL_RULES`, `_PROJECTION_RULES`), the critic's system
# prompt, `format_authoritative_evidence` and the body of `review_prompt` are upstream's, from
# auto_ontology/retrieval/text_to_sql/prompts.py at 2a24f5d, unchanged but for the slots they
# fill: the rules lose the slots for example queries and join paths, which the grounding pack
# shows, and the critic is shown the result's first rows and the draft answer, which upstream's
# intent validation never had. The agent's framing, the tool steps and the pack's rendering are
# Ontological's (ARCHITECTURE.md §8.1). Prompt content is tuned against the eval (BUILD_PLAN.md).

"""The answer agent's system prompt and the critic's prompts (ARCHITECTURE.md §8.1)."""

from __future__ import annotations

import json
from datetime import date

from ontological.answer.models import CALC_SUBTYPES
from ontological.answer.pack import GroundingPack
from ontological.answer.render import render_coverage, render_pack
from ontological.answer.tools import ReviewInput

# The prompt_version the agent's and the critic's calls are logged under. Bump it with an edit.
AGENT_PROMPT_VERSION = "m4.3-1"
REVIEW_PROMPT_VERSION = "2a24f5d"  # upstream's intent validation, as ported

# Spike S1: without this line Haiku gave up after a refusal instead of doing the missing step.
REFUSAL_RULE = (
    "A tool may refuse with an error that says which step is missing. When one does, do that "
    "step and call the tool again."
)

DIALECT_NOTE = (
    "**DuckDB**\n"
    "- The SQL runs on DuckDB, with the database attached as the default catalog. Name a "
    "table as schema.table or by its bare name; never prefix it with the database's name.\n\n"
)

_PROJECTION_RULES = (
    "**Projection**\n"
    "- SELECT only the columns explicitly asked; extra columns make the result "
    "wrong even when the rows are right. For superlative/ranking questions "
    "(most/least/top/highest/lowest/peak/best/worst), select ONLY the item named "
    "the ranking key OR the aggregated value, never both, and never add the "
    "ORDER BY metric unless its value is asked. To identify an entity "
    "(who/which/what), return one identifying column (name if it exists, else id), "
    "not both.\n"
    "- If the user asks for name, project the requested name fields and never add "
    "IDs unless the user explicitly asks for them.\n"
    "- Preserve the question's field order in SELECT: project explicitly requested "
    "outputs from left to right in the same order the user names them.\n"
    "- If evidence maps an answer concept to specific columns, preserve that "
    "projection exactly; do not collapse, reshape, or replace those columns "
    "unless the question explicitly asks for a transformed value.\n"
)

SQL_RULES = (
    "## Rules\n\n"
    "**Correctness**\n"
    "- Every alias used in SELECT / WHERE / GROUP BY / ORDER BY / HAVING "
    "must be defined in FROM or JOIN. Never reference an undefined alias.\n"
    "- Verify each column exists in the table you reference it from. "
    "Do not confuse columns across tables.\n"
    "- GROUP BY must include all non-aggregated columns in SELECT.\n"
    "- ORDER BY must only reference aggregated aliases or columns "
    "present in SELECT/GROUP BY.\n\n"
    "**Joins**\n"
    "- When evidence rules map to columns in different tables, include the joins "
    "that connect those tables using the listed paths. Choose the join type "
    "(INNER / LEFT / RIGHT) from the question's intent and avoid fan-out from "
    "many-to-many joins.\n"
    "- A listed join path is indivisible. If it contains intermediate or bridge "
    "tables, include every table and every hop exactly as listed. Removing a "
    "bridge table, skipping a hop, or replacing the path with a direct endpoint "
    "join is forbidden, even when the bridge contributes no projected column.\n\n"
    "**Aggregation**\n"
    "- Never use FILTER (WHERE ...) on aggregates — it is not supported in all dialects. "
    "Use CASE WHEN inside aggregates instead: "
    "COUNT(CASE WHEN condition THEN 1 END) or SUM(CASE WHEN condition THEN 1 ELSE 0 END).\n"
    "- If business categories are specified, use CASE WHEN to classify.\n"
    "- A condition on an aggregated value belongs in HAVING, not WHERE. When the "
    "question qualifies an entity by a total, average or count taken over that "
    "entity's rows ('customers whose consumption is under 30000', 'patients with "
    "two or more abnormal tests'), group by the entity and compare the aggregate "
    "in HAVING. A WHERE predicate tests one row at a time, before grouping, and "
    "answers a different question.\n"
    "- Integer division truncates. Cast to REAL before dividing wherever the "
    "result can be fractional — CAST(SUM(x) AS REAL) / COUNT(y) — including "
    "inside HAVING and before scaling a ratio by 100.\n\n"
    "**Dialect & Syntax**\n"
    "- Never use :: casts, QUALIFY, DISTINCT ON, IS DISTINCT FROM, IS NOT DISTINCT FROM, "
    "GROUP BY ALL, PIVOT, UNPIVOT, "
    "CROSS JOIN LATERAL, LATERAL JOIN, NATURAL JOIN, implicit comma joins, "
    "or any other vendor-specific or non-standard syntax.\n"
    "- Preserve the exact capitalization of values, names, and identifiers "
    "from the user's question.\n\n"
    f"{DIALECT_NOTE}"
    f"{_PROJECTION_RULES}"
    "**Style**\n"
    "- Preserve natural row grain. Use DISTINCT only when the question or authoritative "
    "evidence explicitly asks for distinct, different, unique, or deduplicated results. "
    "A foreign-key marker signals possible fan-out but does not require DISTINCT: "
    "COUNT(fk) counts matching child rows; COUNT(DISTINCT fk) counts referenced entities. "
    "Never infer DISTINCT solely from an FK, one-to-many join, repeated IDs, or an entity "
    "noun.\n"
    "- NULL filtering: Do not add `IS NOT NULL` solely because a projected field "
    "has `is_nullable: true`, or because one or multiple fields are projected. "
    "Add it only when the question or evidence requires the requested value to "
    "exist. If the concept maps to one nullable column, check that column; if it "
    "maps to a composite concept, check every required nullable component. "
    "Otherwise, preserve rows containing NULLs.\n"
    "- Time windows: apply a date/year filter ONLY when the question's data "
    "request names a period; 'last week/month/year' then means the most "
    "recent completed calendar period, not a rolling window.\n"
    "- Infer LIMIT from the question's intent: "
    "if a superlative (most/least/highest/lowest/best/worst/top/bottom) "
    "is paired with a number, add LIMIT with that number; "
    "if an unnumbered superlative asks for one singular item, add LIMIT 1; "
    "if it asks for plural items, do not add LIMIT; "
    "if a specific count is requested without a superlative, "
    "add LIMIT with that number; "
    "otherwise do not add LIMIT.\n"
    "- Do NOT include comments in the SQL.\n"
    "- Do NOT use ellipsis as placeholder — output the complete SQL.\n"
)


def agent_system_prompt(pack: GroundingPack, *, critic: bool, today: date | None = None) -> str:
    today = today or date.today()
    review_step = (
        "6. Call review_answer with the validation_id and a draft of your answer. If the "
        "critic finds issues, fix the SQL and go through validate_sql, run_sql and "
        "review_answer again.\n"
        if critic
        else ""
    )
    ids = "plan_id, validation_id and review_id" if critic else "plan_id and validation_id"
    last = 7 if critic else 6
    subtypes = "\n".join(f"- {name}: {about}" for name, about in CALC_SUBTYPES.items())
    if pack.coverage:
        where = (
            "You answer one question from one of the databases below by calling tools, then "
            "return the answer in the JSON schema you were given. The question named no "
            "database, so retrieval ranked them by how much of it each covers:\n"
            f"{render_coverage(pack)}\n"
            f"The grounding pack below is {pack.database}'s. If the question belongs to "
            "another of these, name it in plan's database; the tools then work against it.\n"
        )
    else:
        where = (
            f"You answer one question about the {pack.database} database by calling tools, "
            "then return the answer in the JSON schema you were given.\n"
        )
    return (
        f"{where}"
        f"Today's date is {today.isoformat()}.\n\n"
        "## Steps\n"
        "1. Call plan first: the question's intent, its calculation subtype, the entities, "
        "evidence and subject in it, and the anchor attribute from the grounding pack when one "
        "fits. The intent is calculation for a question the data answers with SQL, "
        "information for a question about what the data holds (its tables, columns, Terms and "
        "values), and prediction for a forecast of what hasn't happened.\n"
        "2. Read the candidates plan returns beside the grounding pack below. When they don't "
        "hold what the question needs, call search_ontology, get_term and describe_table; "
        "join_path for how two tables join; column_values for how a value is stored.\n"
        "3. Write one DuckDB SELECT and call validate_sql. If it finds issues, fix the SQL "
        "and call it again.\n"
        "4. Call run_sql with the validation_id. If the source refuses the SQL, fix it and "
        "validate it again.\n"
        "5. If run_sql returns no rows, recheck the filters and literals before you accept "
        "that.\n"
        f"{review_step}"
        f"{last}. Return the answer, with the SQL exactly as "
        f"validate_sql passed it and the {ids} the tools returned. An answer whose steps "
        "don't match is shown to the user as unverified.\n\n"
        "An information question is answered from the ontology alone, with no SQL: run_sql "
        "and review_answer are withheld for it. Ontological doesn't do predictions: for one, "
        "plan says how to end the session. When the data can't answer the question, return "
        "answerable=false, say in missing what is missing, and give no SQL.\n"
        f"{REFUSAL_RULE}\n\n"
        "## Calculation subtypes\n"
        f"{subtypes}\n\n"
        "## Grounding pack\n"
        "What retrieval found for the question in the ontology. Use ONLY the tables and "
        "columns the pack and the tools show; do NOT invent tables, schemas, or columns.\n\n"
        f"{render_pack(pack)}\n\n"
        f"{SQL_RULES}"
    )


# --- the critic --------------------------------------------------------------------------------

INTENT_VALIDATION_SYSTEM_PROMPT = """You are a SQL
validation expert. Your job is to check if a generated
SQL query has any CRITICAL issues that would prevent it
from answering the user's question.

Be LENIENT - only mark as invalid if there are serious
problems. Minor issues or alternative approaches are
acceptable.

Check for CRITICAL issues only:
1. **Seriously Wrong Joins**: Flag only joins that are
nonsensical or clearly break the question (e.g. joining
unrelated tables, inventing keys). Alternate but plausible
join paths that still answer the question are acceptable —
including a different entity for a filter dimension, a
different field/role for the same concept, a
shorter/longer path, or another valid FK chain. Do NOT
fail for those.
2. **Clearly Wrong Aggregations**: Are aggregations
completely incorrect? (e.g., COUNT when user explicitly
asks for SUM) (Minor variations are acceptable)

When DOMAIN-SPECIFIC CUSTOM ANALYSES are provided, treat
their SQL patterns as intentional user-defined domain
definitions. Fragments that look unusual, incomplete, or
nonstandard in isolation are still valid if they follow
those custom analyses — do NOT mark them as critical issues
solely for that reason.

When AUTHORITATIVE JOIN PATHS are provided, they come from
the verified semantic model. If the generated SQL uses a
join condition from those paths, keep it and do NOT flag
that join as invalid.

IMPORTANT: Be generous in your validation. If the SQL
could reasonably answer the question, mark it as valid.
Only fail validation for serious, critical errors that
would make the query unusable."""


def format_authoritative_evidence(evidence: str) -> str:
    """Wrap evidence verbatim with strict instructions for SQL use and validation."""
    if not evidence:
        return ""
    return (
        "## Authoritative Evidence\n"
        "This evidence is critical. The generated SQL MUST follow every instruction "
        "in it exactly, even when another approach appears equivalent.\n"
        "- Whenever the evidence explains a value or where that value is stored, "
        "include the value in the SQL using the stated column, table, filter, and "
        "operator. Do not substitute another value or location.\n"
        "- Whenever the evidence explains a formula, calculate that exact formula "
        "from the stated available tables and columns. Do not replace it with a "
        "shortcut or precomputed field. Preserve the evidence's stated order of "
        "all formula terms, operands, predicates, and conditions exactly; never "
        "reorder them, even when the reordered expression appears equivalent.\n\n"
        "Evidence (verbatim):\n"
        f"{evidence}"
    )


def review_system_prompt(review: ReviewInput) -> str:
    evidence = format_authoritative_evidence("\n".join(review.plan.get("evidence") or []))
    return INTENT_VALIDATION_SYSTEM_PROMPT + (f"\n\n{evidence}" if evidence else "")


def review_prompt(review: ReviewInput, *, join_paths: str = "") -> str:
    """Upstream's `create_intent_validation_prompt`, with the result and the draft answer."""
    join_paths_block = (
        f"\nAUTHORITATIVE JOIN PATHS (keep joins that follow these verified paths):\n{join_paths}\n"
        if join_paths
        else ""
    )
    evidence_criterion = (
        "\n3. Does the SQL follow EVERY instruction in the Authoritative Evidence "
        "exactly? Evidence compliance is strict, not lenient. Flag any missing or "
        "substituted evidence-defined value, column/table mapping, filter, operator, "
        "or formula as a critical evidence issue."
        if review.plan.get("evidence")
        else ""
    )
    authoritative_note = (
        "\nIf AUTHORITATIVE JOIN PATHS are listed above, do not flag a generated join that "
        "follows one of those verified paths."
    )
    used = (
        "\nUSED SQL OBJECTS "
        "(catalog metadata only for tables and columns referenced by the query):\n"
        f"Tables: {', '.join(review.tables)}\nColumns: {', '.join(review.used_columns)}\n"
    )
    shown = len(review.rows)
    more = " (more rows were cut off)" if review.truncated else ""
    result = (
        f"\nResult ({review.row_count} row(s){more}; the first {shown} shown):\n"
        f"Columns: {json.dumps(review.columns, ensure_ascii=False)}\n"
        + "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in review.rows)
        + "\n"
    )
    return f"""User's Question:
{review.question}
{join_paths_block}
{used}
Generated SQL Query:
```sql
{review.sql}
```
{result}
Draft answer:
{review.answer_draft}

Check for CRITICAL issues ONLY (be lenient):
1. Are any joins nonsensical or clearly broken for the question? Alternate but plausible \
join paths that could still answer it are OK — including different fields/roles for the same \
concept (e.g. customer vs supplier delivery city for a region filter). Do NOT fail for those.
2. Are aggregations CLEARLY WRONG for the question? (e.g., COUNT when explicitly asking for SUM) \
(Variations are OK)
{evidence_criterion}

Only mark as invalid if there are SERIOUS problems. If the SQL could reasonably work, mark it \
as VALID.{authoritative_note}

Provide your analysis."""
