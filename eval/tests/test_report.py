"""The run report and its arithmetic, on a synthetic run.json."""

from __future__ import annotations

from typing import Any

from ontological_eval import report, runs

SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"
ROUTES = {
    "answer_agent": {"model": SONNET, "effort": "medium"},
    "answer_review": {"model": SONNET, "effort": "medium"},
}


def usage(model_in: int, side_in: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {
        SONNET: {"inputTokens": model_in, "outputTokens": 10, "cacheReadInputTokens": 100,
                 "cacheCreationInputTokens": 5, "costUSD": 0.01},
    }  # fmt: skip
    if side_in:
        out[HAIKU] = {"inputTokens": side_in, "outputTokens": 1, "costUSD": 0.0001}
    return out


def call(task: str = "answer_agent", turns: int | None = 6, **over: Any) -> runs.Call:
    c: runs.Call = {
        "task": task, "kind": "agent", "model": SONNET, "effort": "medium", "outcome": "ok",
        "error": None, "resolved_model": SONNET, "answered_model": SONNET, "fallback": False,
        "usage": usage(1000, side_in=50), "cost_usd": 0.01, "duration_ms": 1000,
        "num_turns": turns, "cached": False,
    }  # fmt: skip
    return {**c, **over}


def scored(match: bool, failure: str | None = None, artifact: bool = False) -> dict[str, Any]:
    return {"match": match, "failure": failure, "dialect_artifact": artifact, "gold_rows": 1,
            "pred_rows": 1, "error": None, "gold_sample": [["2012-03-01"]],
            "pred_sample": [["2012-03-01"]]}  # fmt: skip


def agent(key: str, db: str, match: bool, *, verified: bool = True, **over: Any) -> runs.Record:
    source, qid = key.split(":")
    rec: runs.Record = {
        "key": key, "source": source, "id": int(qid), "db": db,
        "difficulty": "" if source == "wwi" else "challenging",
        "verified": verified, "reason": None if verified else "run_sql never ran this SQL",
        "outcome": "ok", "error": None, "answered_model": SONNET, "answer": "x",
        "answerable": True, "sql": "SELECT 1", "sql_validated": True, "steps": ["plan"],
        "score": scored(match, None if match else "wrong_result"),
        "calls": [call(), call("answer_review", turns=1, kind="structured", usage=usage(200))],
        "other_calls": [], "seconds": 40.0,
    }  # fmt: skip
    return {**rec, **over}


def control(key: str, db: str, match: bool) -> runs.Record:
    source, qid = key.split(":")
    return {
        "key": key, "source": source, "id": int(qid), "db": db, "difficulty": "",
        "model": SONNET, "effort": "medium", "outcome": "ok", "error": None, "sql": "SELECT 1",
        "score": scored(match, None if match else "wrong_result"),
        "calls": [call(kind="structured", turns=2, usage=usage(3000))], "seconds": 9.0,
    }  # fmt: skip


def reading(five_hour: float, seven_day: float) -> dict[str, Any]:
    return {"at": "", "status": "allowed",
            "windows": {"five_hour": five_hour, "seven_day": seven_day}}  # fmt: skip


def a_run(**over: Any) -> dict[str, Any]:
    agent_records = [
        agent("wwi:3", "wideworldimporters", True),
        agent("wwi:5", "wideworldimporters", False, verified=False),
        agent("bird:10", "financial", True),
        agent("bird:11", "financial", False,
              score=scored(False, "wrong_result", artifact=True)),
    ]  # fmt: skip
    run: dict[str, Any] = {
        "run_id": "20261107T120000Z-smoke-sonnet", "set": "smoke", "config": "sonnet",
        "critic": True, "routes": ROUTES, "questions_in_set": 20,
        "questions": [r["key"] for r in agent_records],
        "started_at": "2026-11-07T12:00:00+00:00", "finished_at": "2026-11-07T12:05:00+00:00",
        "stopped": None, "error": None, "engine": {"pid": 1, "version": "0"},
        "utilization": {
            "start": reading(0.10, 0.30),
            "after_agent": reading(0.14, 0.31),
            "after_control": reading(0.15, 0.31),
        },
        "agent": agent_records,
        "control": {"model": SONNET, "effort": "medium", "file": "", "asked_this_run": 4,
                    "records": [control("wwi:3", "wideworldimporters", False),
                                control("wwi:5", "wideworldimporters", True),
                                control("bird:10", "financial", True),
                                control("bird:11", "financial", False)]},
    }  # fmt: skip
    return {**run, **over}


def test_tokens_split_the_answering_model_from_claude_code_s_side_calls() -> None:
    t = runs.tokens([call(), call()])
    assert t["answering"][SONNET]["input"] == 2000
    assert t["answering"][SONNET]["cache_read"] == 200
    assert t["side"][HAIKU]["input"] == 100
    assert HAIKU not in t["answering"] and SONNET not in t["side"]
    # A Haiku session's own Haiku entry is the answering model's.
    t = runs.tokens([call(answered_model=HAIKU, resolved_model=HAIKU, usage=usage(0, 70))])
    assert t["answering"][HAIKU]["input"] == 70


def test_spawns_skip_cache_hits_and_turns_add_up() -> None:
    calls = [call(turns=6), call(turns=1, cached=True), call(turns=None)]
    assert runs.spawns(calls) == 2
    assert runs.turns(calls) == 7


def test_summary_has_the_metrics_scoring_and_runs_lists() -> None:
    s = report.summarize(a_run())
    assert s["questions"] == 4
    assert s["execution_accuracy"] == (2, 4)
    assert s["control_accuracy"] == (2, 4)
    assert s["verified"] == 3
    assert s["spawns_per_question"] == 2.0
    assert s["turns_per_question"] == 7.0
    assert s["tokens"]["side"][HAIKU]["input"] == 4 * 50
    assert s["control_tokens"]["answering"][SONNET]["input"] == 4 * 3000
    change = s["utilization_change"]
    assert round(change["agent"]["five_hour"], 6) == 0.04
    assert round(change["control"]["five_hour"], 6) == 0.01
    assert s["healthy"]


def test_the_report_shows_accuracy_flips_the_gate_usage_and_models() -> None:
    md = report.markdown(a_run())
    assert "| All | 50.0% (2/4) | 50.0% (2/4) |" in md
    assert "| BIRD financial | 50.0% (1/2) | 50.0% (1/2) |" in md
    assert "Only the agent got right: wwi:3" in md
    assert "Only the control got right: wwi:5" in md
    assert "within the ~10-point noise" in md
    assert "Verified by the post-run check: 75.0% (3/4)" in md
    assert "## Dialect artifacts (1)" in md and "- bird:11 (financial)" in md
    assert f"| agent | {HAIKU} | side calls |" in md
    assert "five_hour +0.04" in md
    assert f"| agent | answer_agent | {SONNET} | {SONNET} (4) | {SONNET} (4) |" in md
    assert "Not pinned" not in md and "Not a score" not in md


def test_the_report_counts_tolerated_matches_and_names_them() -> None:
    run = a_run()
    run["agent"][3]["score"] = scored(True, artifact=True)
    md = report.markdown(run)
    assert "| All | 75.0% (3/4) |" in md
    assert "## Dialect artifacts (1)" in md and "- bird:11 (financial): match" in md
    # A run scored before the M1.10 rule kept its artifacts as misses, and still reads so.
    assert "- bird:11 (financial): wrong_result" in report.markdown(a_run())


def test_a_run_mostly_without_answers_is_not_a_score() -> None:
    run = a_run()
    for r in run["agent"][:2]:
        r["score"] = scored(False, "no_answer")
    md = report.markdown({**run, "stopped": "Claude's rate limit, at wwi:5"})
    assert "**Not a score for the agent:** 50% of its questions got no answer" in md
    assert "**Stopped:** Claude's rate limit" in md
    assert not report.summarize(run)["healthy"]


def test_calls_off_the_pinned_routes_and_other_tasks_are_flagged() -> None:
    run = a_run()
    run["agent"][0]["calls"].append(call(model="sonnet"))
    run["agent"][1]["other_calls"] = ["table_terms"]
    md = report.markdown(run)
    assert "**Not pinned:** answer_agent ran on route model sonnet" in md
    assert "aren't answering's while the run asked: table_terms (1)" in md


def test_a_run_without_answers_or_control_still_reports() -> None:
    md = report.markdown(a_run(agent=[], control=None, error="RunError: no engine: boom"))
    assert "No question was answered." in md and "**Failed:** RunError" in md
    md = report.markdown(a_run(control=None))
    assert "| All | 50.0% (2/4) | - |" in md
