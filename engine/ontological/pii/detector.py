# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by Ontological: ported from upstream's auto_ontology/pii_detection/detector.py at
# 2a24f5d. The rules come first, as upstream's do, and the system prompt and the answer's rules
# (an unsure "review" counts as PII) are upstream's. Upstream asks its LLM once per uncertain
# column, six at a time; here up to 40 uncertain columns share one call on the `pii_classify`
# route, one call at a time, and a column the answer leaves out is asked alone.

"""Rules-first PII classification with a batched Claude fallback (ARCHITECTURE.md §1.2, §6.4).

Claude sees metadata only, as upstream's LLM does: a column's name, its table's name and its
description, never its values. A column whose call answered invalidly gets a `fallback`
decision, which the caller leaves unprocessed for a later run.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ontological.compile.gateway import Structured, ask
from ontological.pii.models import ColumnInput, PiiDecision, PiiStatus
from ontological.pii.rules import evaluate_rules

TASK = "pii_classify"
PROMPT_VERSION = "2a24f5d"
# The batched prompt is Ontological's, so it has a version of its own.
BATCH_PROMPT_VERSION = "2a24f5d+batch.1"
#: Uncertain columns to a call (ARCHITECTURE.md §6.3).
BATCH = 40

SYSTEM = """\
You classify database columns for personally identifiable information (PII).
Use the column name, table name, and description together.
PII is information that identifies a person directly or can identify them when combined
with other data. Do not classify business, product, or aggregate information as PII.
If context is insufficient or you are unsure whether the column is PII, classify it as
"pii". This is a conservative policy: uncertainty must be treated as PII, not "review".
Always return status, category, confidence, and a short reason.
Never include hidden reasoning."""

BATCH_NOTE = """

This request is a JSON list of columns, each with an "id". Classify each column on its own, \
and return one entry per column in `columns`, with `id` set to that column's id."""


class PiiResponse(BaseModel):
    """Upstream's `_PiiResponse`: the structured answer for one column."""

    model_config = ConfigDict(extra="forbid")

    status: PiiStatus = Field(description="pii, not_pii, or review")
    category: str | None = Field(default=None, description="Short snake_case PII category, or null")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence from 0 to 1")
    reason: str = Field(default="", description="One short explanation")

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        return value.strip()


class PiiColumnEntry(PiiResponse):
    id: int = Field(..., description="The column's id, as given in the request.")


class PackedPiiResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[PiiColumnEntry] = Field(..., description="One entry per column in the request.")


def payload(column: ColumnInput) -> dict[str, str | None]:
    return {
        "column_name": column.column_name,
        "table_name": column.table_name,
        "description": column.description,
    }


def decision(response: PiiResponse) -> PiiDecision:
    """Upstream's reading of an answer: "review" is PII with full confidence, and PII with no
    category is `other`."""
    status, category, confidence = response.status, response.category, response.confidence
    if status is PiiStatus.REVIEW:
        status, confidence = PiiStatus.PII, 1.0
    if status is PiiStatus.PII and not category:
        category = "other"
    return PiiDecision(
        status=status,
        category=category,
        confidence=confidence,
        reason=response.reason or f"LLM classified the column as {status.value}.",
        source="llm",
    )


INVALID = PiiDecision(
    status=PiiStatus.REVIEW,
    category=None,
    confidence=0.0,
    reason="Classifier failed: invalid answer.",
    source="fallback",
)

UNCONFIGURED = PiiDecision(
    status=PiiStatus.REVIEW,
    category=None,
    confidence=0.0,
    reason="No deterministic rule matched and no classifier is configured.",
    source="fallback",
)


async def classify(gateway: Structured, column: ColumnInput) -> PiiDecision:
    """Upstream's `LlmPiiClassifier.classify`: one column, upstream's prompt. Raises
    `ClaudeUnavailable` when Claude doesn't answer."""
    response = await ask(
        gateway, TASK, SYSTEM, json.dumps(payload(column)), PiiResponse, PROMPT_VERSION
    )
    return INVALID if response is None else decision(response)


async def classify_batch(gateway: Structured, columns: Sequence[ColumnInput]) -> list[PiiDecision]:
    """Each column's decision, from one call for several columns. A column the answer leaves
    out, and every column of an invalid answer, is asked alone. Raises `ClaudeUnavailable`."""
    if len(columns) == 1:
        return [await classify(gateway, columns[0])]
    request = [{"id": i, **payload(c)} for i, c in enumerate(columns, start=1)]
    result = await ask(
        gateway,
        TASK,
        SYSTEM + BATCH_NOTE,
        json.dumps(request),
        PackedPiiResponse,
        BATCH_PROMPT_VERSION,
    )
    found: dict[int, PiiColumnEntry] = {}
    for entry in result.columns if result is not None else []:
        found.setdefault(entry.id, entry)
    out: list[PiiDecision] = []
    for i, column in enumerate(columns, start=1):
        entry = found.get(i)
        out.append(await classify(gateway, column) if entry is None else decision(entry))
    return out


def rules_first(
    columns: Sequence[ColumnInput],
) -> tuple[dict[ColumnInput, PiiDecision], list[ColumnInput]]:
    """The decisions the rules make, and the distinct columns they leave to Claude, in order."""
    decided: dict[ColumnInput, PiiDecision] = {}
    uncertain: list[ColumnInput] = []
    for column in dict.fromkeys(columns):
        found = evaluate_rules(column)
        if found is None:
            uncertain.append(column)
        else:
            decided[column] = found
    return decided, uncertain
