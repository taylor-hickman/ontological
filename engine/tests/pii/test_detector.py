"""The batched Claude fallback for PII (BUILD_PLAN.md M3.5): uncertain columns share a call, a
column the answer leaves out is asked alone, and upstream's reading of an answer holds. The
gateway is scripted."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fakes import NoAnswer, ScriptedGateway

from ontological.compile.gateway import ClaudeUnavailable
from ontological.pii import detector
from ontological.pii.models import ColumnInput, PiiStatus

COLUMNS = [
    ColumnInput("nickname", "accounts", "What the account holder goes by."),
    ColumnInput("loyalty_tier", "accounts"),
    ColumnInput("badge_code", "staff"),
]


def answer_all(task: str, user: str) -> dict[str, Any]:
    request = json.loads(user)
    if isinstance(request, dict):  # one column, upstream's prompt
        return {"status": "not_pii", "confidence": 0.7, "reason": "alone"}
    return {
        "columns": [
            {"id": c["id"], "status": "pii" if c["column_name"] == "nickname" else "not_pii",
             "category": None, "confidence": 0.95, "reason": f"about {c['column_name']}"}
            for c in request
        ]
    }  # fmt: skip


def test_uncertain_columns_share_one_call_with_metadata_only() -> None:
    gateway = ScriptedGateway(answer_all)

    got = asyncio.run(detector.classify_batch(gateway, COLUMNS))

    [call] = gateway.calls
    assert (call.task, call.prompt_version) == ("pii_classify", "2a24f5d+batch.1")
    assert call.system == detector.SYSTEM + detector.BATCH_NOTE
    assert json.loads(call.user) == [
        {"id": 1, "column_name": "nickname", "table_name": "accounts",
         "description": "What the account holder goes by."},
        {"id": 2, "column_name": "loyalty_tier", "table_name": "accounts", "description": None},
        {"id": 3, "column_name": "badge_code", "table_name": "staff", "description": None},
    ]  # fmt: skip
    assert [(d.status, d.category, d.source) for d in got] == [
        (PiiStatus.PII, "other", "llm"),  # PII with no category is `other`, as upstream's is
        (PiiStatus.NOT_PII, None, "llm"),
        (PiiStatus.NOT_PII, None, "llm"),
    ]
    assert got[1].reason == "about loyalty_tier"


def test_a_column_the_answer_leaves_out_is_asked_alone_with_upstream_s_prompt() -> None:
    def leave_out_badge(task: str, user: str) -> Any:
        found = answer_all(task, user)
        if "columns" in found:
            found["columns"] = [c for c in found["columns"] if c["id"] != 3]
        return found

    gateway = ScriptedGateway(leave_out_badge)

    got = asyncio.run(detector.classify_batch(gateway, COLUMNS))

    assert [c.prompt_version for c in gateway.calls] == ["2a24f5d+batch.1", "2a24f5d"]
    alone = gateway.calls[1]
    assert alone.system == detector.SYSTEM
    assert json.loads(alone.user) == {
        "column_name": "badge_code", "table_name": "staff", "description": None,
    }  # fmt: skip
    assert got[2].reason == "alone"


def test_an_invalid_answer_asks_each_column_alone_and_invalid_alone_is_left_for_later() -> None:
    def invalid_batch(task: str, user: str) -> Any:
        request = json.loads(user)
        if isinstance(request, list):
            return None
        if request["column_name"] == "badge_code":
            return None
        return {"status": "review", "confidence": 0.2}

    gateway = ScriptedGateway(invalid_batch)

    got = asyncio.run(detector.classify_batch(gateway, COLUMNS))

    assert len(gateway.calls) == 4
    # "review" counts as PII with full confidence (upstream's conservative policy)
    assert [(d.status, d.confidence, d.category) for d in got[:2]] == [
        (PiiStatus.PII, 1.0, "other"),
        (PiiStatus.PII, 1.0, "other"),
    ]
    assert got[2] == detector.INVALID and got[2].source == "fallback"


def test_no_answer_raises_for_the_job_to_pause() -> None:
    gateway = ScriptedGateway(lambda task, user: NoAnswer())
    with pytest.raises(ClaudeUnavailable):
        asyncio.run(detector.classify_batch(gateway, COLUMNS))


def test_the_rules_decide_first_and_leave_each_distinct_uncertain_column_once() -> None:
    email = ColumnInput("email", "customers")
    decided, uncertain = detector.rules_first([email, COLUMNS[0], COLUMNS[0], COLUMNS[1]])
    assert list(decided) == [email]
    assert uncertain == COLUMNS[:2]
