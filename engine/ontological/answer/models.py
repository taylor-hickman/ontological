# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: `Review` is upstream's `IntentValidationModel` from
# auto_ontology/retrieval/text_to_sql/agents/sql_parse_validation.py at 2a24f5d, with its field
# descriptions, and `passed` and `issues` hold upstream's rule for reading it. The calculation
# subtypes are the names and descriptions in upstream's agents/calculation_subtypes.json.
# `AnswerModel` is Ontological's (ARCHITECTURE.md §8.1).

"""What the answer agent ends with, and what its critic says (ARCHITECTURE.md §8.1)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Intent = Literal["calculation", "information", "prediction"]

# Upstream's nine calculation subtypes, each a kind of SQL the question needs
CALC_SUBTYPES: dict[str, str] = {
    "match_based": "Retrieve rows matching explicit values or conditions.",
    "ranking": "Request top or bottom N records, or order records by a measure.",
    "comparison": "Compare a value with another value, threshold, or condition.",
    "counting": "Count records or distinct values that satisfy conditions.",
    "aggregation": "Apply a standard aggregate such as sum, average, minimum, or maximum.",
    "domain_knowledge": (
        "Apply a domain rule, specialized threshold, or domain-specific interpretation."
    ),
    "numeric_computation": "Perform arithmetic, a ratio, percentage, or another derived formula.",
    "synonym": "Map an alternate term in the question to a stored concept or value.",
    "value_illustration": (
        "Use an example or descriptive phrase to identify the corresponding stored value."
    ),
}


class AnswerModel(BaseModel):
    """The session's final answer, carried by `output_format`. The ids tie it to the tool calls
    the post-run check reads."""

    answer: str = Field(description="The answer for the user, in plain English.")
    sql: str | None = Field(
        default=None,
        description="The SQL the answer comes from, exactly as validate_sql passed it. "
        "None when there is no SQL: an information answer, or answerable=false.",
    )
    plan_id: str = Field(description="The plan_id plan returned for this answer.")
    validation_id: str | None = Field(
        default=None, description="The validation_id validate_sql returned for the SQL."
    )
    review_id: str | None = Field(
        default=None, description="The review_id review_answer returned for the SQL."
    )
    assumptions: list[str] = Field(
        default_factory=list[str],
        description="Each choice the question or data didn't determine, with its reason.",
    )
    terms_used: list[str] = Field(
        default_factory=list[str], description="The ontology Terms the answer relies on."
    )
    answerable: bool = Field(
        description="False when the ontology or the data can't answer the question."
    )
    missing: list[str] = Field(
        default_factory=list[str],
        description="When answerable is false: what is missing to answer it.",
    )


class Review(BaseModel):
    """Upstream's intent validation: the critic's structured verdict on one query."""

    reasoning: str = Field(
        default="",
        description=(
            "Brief reasoning (1-2 sentences max) on whether the SQL addresses "
            "the question's intent."
        ),
    )
    is_valid: bool = Field(
        description=(
            "Whether the SQL query has any CRITICAL issues. Should be True "
            "unless there are serious problems."
        )
    )
    join_issues: list[str] = Field(
        default_factory=list[str],
        description=(
            "List of CRITICAL join issues that would produce completely wrong "
            "results. Leave EMPTY [] if there are no join issues — do NOT add "
            "explanatory text like 'no join issues'."
        ),
    )
    aggregation_issues: list[str] = Field(
        default_factory=list[str],
        description=(
            "List of CRITICAL aggregation issues that are clearly wrong (not "
            "minor variations). Leave EMPTY [] if there are no aggregation "
            "issues — do NOT add explanatory text like 'no aggregation issues'."
        ),
    )
    projection_issues: list[str] = Field(
        default_factory=list[str],
        description=(
            "When strict short-answer validation is requested, list every extra, "
            "missing, duplicated, reordered, or reshaped output field compared with "
            "the exact projection requested by the question and evidence. Leave "
            "EMPTY [] only when the projection matches exactly."
        ),
    )
    evidence_issues: list[str] = Field(
        default_factory=list[str],
        description=(
            "List every way the SQL violates Authoritative Evidence, including a "
            "missing or substituted value, column/table mapping, filter, operator, "
            "or formula. Leave EMPTY [] when all evidence instructions are followed."
        ),
    )

    @field_validator(
        "join_issues",
        "aggregation_issues",
        "projection_issues",
        "evidence_issues",
        mode="before",
    )
    @classmethod
    def _empty_string_means_no_issues(cls, value: Any) -> Any:
        """Treat the model's occasional empty-string issue list as empty."""
        if value == "":
            return []
        return value

    @property
    def issues(self) -> list[str]:
        return [
            f"{kind}: {issue}"
            for kind, found in (
                ("join", self.join_issues),
                ("aggregation", self.aggregation_issues),
                ("projection", self.projection_issues),
                ("evidence", self.evidence_issues),
            )
            for issue in found
        ]

    @property
    def passed(self) -> bool:
        """Upstream's rule: valid with no evidence or projection issues passes, and so does
        invalid with no issues named at all."""
        if self.is_valid and not self.evidence_issues and not self.projection_issues:
            return True
        return not self.issues
