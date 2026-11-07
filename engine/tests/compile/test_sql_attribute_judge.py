"""The SQL-attribute judge (BUILD_PLAN.md M3.5): `_usage_score` fixed, so fragments rank by their
statements' recent uses, and several Terms judged to a call. The gateway is scripted."""

from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any

import pytest
from fakes import NoAnswer, ScriptedGateway

from ontological.compile import sql_attributes as judge
from ontological.compile.gateway import ClaudeUnavailable
from ontological.compile.sql_attributes import Ranked, TermWork


def test_the_window_is_the_three_months_ending_with_the_history_s_latest() -> None:
    assert judge.usage_window([date(2025, 12, 1), date(2026, 11, 1), date(2026, 9, 1)]) == date(
        2026, 9, 1
    )
    assert judge.usage_window([date(2026, 2, 1)]) == date(2025, 12, 1)
    assert judge.usage_window([]) is None


def test_a_statement_scores_its_uses_inside_the_window() -> None:
    months = {date(2026, 11, 1): 3, date(2026, 9, 1): 2, date(2026, 8, 1): 50}
    assert judge.usage_score(months, date(2026, 9, 1)) == 5
    assert judge.usage_score(months, None) == 0


def statement(sql_id: str, text: str, total: int) -> dict[str, Any]:
    return {"sql_id": sql_id, "sql_text": text, "props": {"total_counter": total}}


def test_fragments_rank_by_recent_uses_then_all_time_uses_then_first_seen() -> None:
    # upstream's score was always 0, so these came out in the order the statements were read
    sqls = [
        statement("old", "OLD", 500),
        statement("busy", "BUSY", 7),
        statement("quiet", "QUIET", 1),
    ]
    fragments = {
        "OLD": ["status = 'legacy'", "SUM(total)"],
        "BUSY": ["status = 'active'", "SUM(total)"],
        "QUIET": ["region = 'EU'"],
    }
    usage = {"old": 0, "busy": 6, "quiet": 0}

    got = judge.rank_expressions(sqls, fragments, usage)

    assert [(r.expression, r.score, r.total, r.sql_ids) for r in got] == [
        ("SUM(total)", 6, 507, ["old", "busy"]),
        ("status = 'active'", 6, 7, ["busy"]),
        ("status = 'legacy'", 0, 500, ["old"]),
        ("region = 'EU'", 0, 1, ["quiet"]),
    ]


def term(name: str, *exprs: str) -> TermWork:
    top = [Ranked(e, 3, 3, ["s"]) for e in exprs]
    return TermWork(f"id-{name}", name, f"A {name}.", top, {e: ["SELECT 1"] for e in exprs})


def test_the_prompt_is_upstream_s_with_the_real_counts() -> None:
    prompt = judge.build_prompt("Invoice", "", term("Invoice", "total > 10").top)
    assert prompt.startswith(
        "Business term: Invoice\n"
        "Description: (none)\n\n"
        "Top SQL expressions extracted from queries on tables for this term "
        "(ranked by 3-month execution count):\n\n"
        "1. [3 uses] total > 10\n\n"
        "Which expressions should become a named SqlAttribute for the term 'Invoice'?\n\n"
        "Rules you MUST follow:\n"
    )
    assert prompt.endswith("Return an empty list if none qualify.")


def test_terms_batch_up_to_ten_and_about_24k_characters() -> None:
    many = [term(f"T{i}", "a = 1") for i in range(12)]
    assert [len(b) for b in judge.batches(many)] == [10, 2]

    wide = [term(f"W{i}", "x" * 9_000) for i in range(4)]
    assert [len(b) for b in judge.batches(wide)] == [2, 2]


def suggest_first(task: str, user: str) -> dict[str, Any]:
    """Keep each Term's first expression, named after the Term."""
    heads = re.findall(r"^Business term: (\S+)$", user, flags=re.M)
    first = re.findall(r"^1\. \[\d+ uses\] (.+)$", user, flags=re.M)
    entries = [
        {"term": h, "suggestions": [{"name": f"{h} Rule", "description": "d", "expression": e}]}
        for h, e in zip(heads, first, strict=True)
    ]
    return {"terms": entries} if len(heads) > 1 else {"suggestions": entries[0]["suggestions"]}


def test_several_terms_share_one_call() -> None:
    gateway = ScriptedGateway(suggest_first)
    terms = [term("Invoice", "total > 10"), term("Track", "Milliseconds > 1000")]

    got, calls = asyncio.run(judge.judge_batch(gateway, terms))

    [call] = gateway.calls
    assert (calls, call.task, call.prompt_version) == (1, "sql_attribute_judge", "2a24f5d+judge.1")
    assert call.system == judge.SYSTEM + judge.BATCH_NOTE
    assert call.user.count("Rules you MUST follow:") == 1
    assert [[s.expression for s in found] for found in got] == [
        ["total > 10"],
        ["Milliseconds > 1000"],
    ]


def test_a_term_the_answer_leaves_out_is_asked_alone_with_upstream_s_prompt() -> None:
    def leave_out_track(task: str, user: str) -> dict[str, Any]:
        found = suggest_first(task, user)
        if "terms" in found:
            found["terms"] = [e for e in found["terms"] if e["term"] != "Track"]
        return found

    gateway = ScriptedGateway(leave_out_track)
    terms = [term("Invoice", "total > 10"), term("Track", "Milliseconds > 1000")]

    got, calls = asyncio.run(judge.judge_batch(gateway, terms))

    assert calls == 2
    assert [c.prompt_version for c in gateway.calls] == ["2a24f5d+judge.1", "2a24f5d"]
    assert gateway.calls[1].user == judge.build_prompt("Track", "A Track.", terms[1].top)
    assert got[1][0].name == "Track Rule"


def test_an_invalid_answer_judges_nothing_and_no_answer_raises() -> None:
    gateway = ScriptedGateway(lambda task, user: None)
    got, calls = asyncio.run(judge.judge_batch(gateway, [term("A", "x = 1"), term("B", "y = 2")]))
    assert (got, calls) == ([[], []], 3)

    with pytest.raises(ClaudeUnavailable):
        asyncio.run(judge.judge_batch(ScriptedGateway(lambda t, u: NoAnswer()), [term("A", "x")]))
