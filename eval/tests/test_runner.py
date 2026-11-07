"""The runner's parts that make no Claude call: configs, choosing questions, the control arm's
prompt, and scoring both arms on a small BIRD-shaped file, gold natively and the agent's SQL
through the engine's source executor."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ontological.answer.models import AnswerModel
from ontological.answer.service import Asked
from ontological.llm.routes import Route

from ontological_eval import control, runner, runs, sets
from ontological_eval.home import bird_dir, wwi_dir
from ontological_eval.questions import Question

GOLD = "SELECT name FROM client WHERE district = 'north' ORDER BY name"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    bird = bird_dir(tmp_path)
    bird.mkdir(parents=True)
    conn = sqlite3.connect(bird / "financial.sqlite")
    conn.executescript(
        """
        CREATE TABLE client (client_id INTEGER PRIMARY KEY, name TEXT, district TEXT,
                             joined DATE, balance REAL);
        INSERT INTO client VALUES (1, 'Ann', 'north', '2012-03-01', 10.5),
                                  (2, 'Bo', 'south', '2013-01-01', 2),
                                  (3, 'Cy', 'north', '2014-06-30', 7.25);
        """
    )
    conn.commit()
    conn.close()
    (wwi_dir(tmp_path) / "ddl").mkdir(parents=True)
    (wwi_dir(tmp_path) / "ddl" / "02_tables.sql").write_text("CREATE TABLE sales.orders (id int);")
    (wwi_dir(tmp_path) / "ddl" / "01_schemas.sql").write_text("CREATE SCHEMA sales;")
    return tmp_path


def bird_q(sql: str = GOLD, evidence: str = "north means district = 'north'") -> Question:
    question = "Which clients are in the north?"
    return Question("bird", 7, "financial", question, evidence, sql, "simple")


@pytest.fixture
def scorer(home: Path) -> Iterator[runner.Scorer]:
    gold = sets.GoldRunner(home, timeout_s=5)
    s = runner.Scorer(home, Path("/nonexistent"), gold)
    try:
        yield s
    finally:
        s.close()
        gold.close()


# --- configs ------------------------------------------------------------------------------------


def test_the_shipped_configs_pin_full_model_ids() -> None:
    for name in ("default", "sonnet"):
        config = runs.load_config(name)
        assert config.critic
        assert {"answer_agent", "answer_review"} <= set(config.routes)
    assert runs.load_config("default").agent == Route("claude-opus-5-5", "medium")
    assert runs.load_config("sonnet").agent == Route("claude-sonnet-5-5", "medium")


@pytest.mark.parametrize(
    ("routes", "match"),
    [
        ({"answer_agent": {"model": "sonnet", "effort": "medium"},
          "answer_review": {"model": "claude-sonnet-5-5", "effort": "medium"}}, "alias"),
        ({"answer_agent": {"model": "claude-sonnet-5-5", "effort": "medium"}}, "answer_review"),
        ({"answer_agent": {"model": "claude-sonnet-5-5", "effort": "lots"}}, "effort"),
    ],
)  # fmt: skip
def test_a_config_that_doesnt_pin_its_answer_routes_is_refused(
    tmp_path: Path, routes: dict[str, Any], match: str
) -> None:
    path = tmp_path / "loose.json"
    path.write_text(json.dumps({"routes": routes}))
    with pytest.raises(runs.ConfigError, match=match):
        runs.load_config(str(path))


# --- questions ----------------------------------------------------------------------------------


def test_select_keeps_the_set_s_order_and_takes_named_questions_or_the_first_n() -> None:
    frozen = sets.read()["smoke"]
    qs = {k: Question(k[0], k[1], "db", "", "", "", "") for k in frozen}
    assert [q.key for q in runner.select("smoke", qs)] == frozen
    assert [q.key for q in runner.select("smoke", qs, limit=3)] == frozen[:3]
    last, first = frozen[-1], frozen[0]
    named = runner.select("smoke", qs, only=[f"{last[0]}:{last[1]}", f"{first[0]}:{first[1]}"])
    assert [q.key for q in named] == [first, last]
    with pytest.raises(runner.RunError, match="not in smoke"):
        runner.select("smoke", qs, only=["bird:999999"])
    with pytest.raises(runner.RunError, match="isn't a question"):
        runner.select("smoke", qs, only=["chinook:1"])


def test_the_agent_is_asked_the_question_with_bird_s_evidence() -> None:
    assert runner.ask_text(bird_q()) == (
        "Which clients are in the north?\nEvidence: north means district = 'north'"
    )
    assert runner.ask_text(bird_q(evidence=" ")) == "Which clients are in the north?"


# --- the control arm's prompt -------------------------------------------------------------------


def test_the_control_sees_the_database_s_own_ddl_and_dialect(home: Path) -> None:
    dump = control.schema_dump(home, bird_q())
    assert dump.startswith("BEGIN TRANSACTION;\nCREATE TABLE client")
    assert dump.endswith(";\nCOMMIT;")
    prompt = control.user_prompt(bird_q(), dump)
    assert prompt.index("### Question") < prompt.index("### Evidence") < prompt.index(dump)
    assert "Write a single SQLite query" in prompt

    wwi = Question("wwi", 3, "wideworldimporters", "How many orders?", "", "", "")
    dump = control.schema_dump(home, wwi)
    assert dump.index("-- 01_schemas.sql") < dump.index("-- 02_tables.sql")
    prompt = control.user_prompt(wwi, dump)
    assert "### Evidence" not in prompt and "Write a single PostgreSQL query" in prompt


# --- scoring ------------------------------------------------------------------------------------


def asked(sql: str | None, *, validated: bool = True, outcome: str = "ok") -> Asked:
    answer = None
    if outcome == "ok":
        answer = AnswerModel(
            answer="Ann and Cy", sql=sql, plan_id="p1", validation_id="v1", answerable=True
        )
    return Asked.model_validate(
        {
            "question": "q",
            "database": "financial",
            "answer": answer,
            "verified": validated,
            "reason": None if validated else "no run_sql",
            "sql": sql if validated else None,
            "result": None,
            "steps": [],
            "outcome": outcome,
            "error": None,
            "answered_model": "claude-sonnet-5-5",
        }
    )


def test_the_agent_s_sql_runs_through_the_source_executor_and_matches(
    scorer: runner.Scorer,
) -> None:
    # DuckDB's dialect, which SQLite doesn't speak: proof it ran through the executor.
    duckdb_only = "SELECT name FROM client WHERE district = 'north' AND list_contains([1], 1)"
    got = scorer.agent(bird_q(), duckdb_only, no_answer=False)
    assert got.match and got.failure is None and got.gold_rows == 2


def test_the_agent_s_misses_are_scored_with_their_reason(scorer: runner.Scorer) -> None:
    q = bird_q()
    assert scorer.agent(q, "SELECT name FROM client", no_answer=False).failure == "wrong_result"
    assert scorer.agent(q, "SELECT nope FROM client", no_answer=False).failure == "pred_error"
    assert scorer.agent(q, None, no_answer=True).failure == "no_answer"
    assert scorer.agent(q, None, no_answer=False).failure == "no_sql"


def test_a_date_duckdb_reads_as_date_matches_as_a_dialect_artifact(scorer: runner.Scorer) -> None:
    q = bird_q("SELECT joined FROM client WHERE client_id = 1")
    got = scorer.agent(q, "SELECT joined FROM client WHERE client_id = 1", no_answer=False)
    assert got.match and got.dialect_artifact
    assert got.gold_sample == [["2012-03-01"]] and got.pred_sample == [["2012-03-01"]]


def test_the_control_s_sql_runs_natively(scorer: runner.Scorer) -> None:
    q = bird_q()
    # SQLite's own dialect: `||` concatenation and its quirks, as the control's schema implies.
    assert scorer.control(q, "SELECT name FROM client WHERE district = 'nor' || 'th'",
                          no_answer=False).match  # fmt: skip
    assert scorer.control(q, "SELECT * FROM missing", no_answer=False).failure == "pred_error"


def test_a_run_away_control_query_is_stopped(home: Path) -> None:
    gold = sets.GoldRunner(home, timeout_s=0.2)
    try:
        s = runner.Scorer(home, Path("/nonexistent"), gold)
        forever = (
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT max(i) FROM n"
        )
        got = s.control(bird_q(), forever, no_answer=False)
        assert got.failure == "pred_error" and "interrupted" in (got.error or "")
    finally:
        gold.close()


def test_the_scored_sql_is_the_validated_sql_else_the_answer_s() -> None:
    assert runner.predicted_sql(asked("SELECT 1")) == ("SELECT 1", True)
    assert runner.predicted_sql(asked("SELECT 2", validated=False)) == ("SELECT 2", False)
    assert runner.predicted_sql(asked(None, outcome="limited")) == (None, False)


def test_an_agent_record_keeps_only_answering_s_calls() -> None:
    calls: list[runs.Call] = [
        {"task": "answer_agent", "num_turns": 7, "cached": False, "usage": {}},
        {"task": "table_terms", "num_turns": 1, "cached": False, "usage": {}},
    ]
    scored = runner.verify.score([(1,)], [(1,)])
    rec = runner.agent_record(bird_q(), asked("SELECT 1"), calls, scored, 12.34)
    assert [c["task"] for c in rec["calls"]] == ["answer_agent"]
    assert rec["other_calls"] == ["table_terms"]
    assert rec["key"] == "bird:7" and rec["sql_validated"] and rec["seconds"] == 12.3
    json.dumps(rec)


def test_the_control_asks_again_where_it_stored_no_answer() -> None:
    qs = [Question("bird", i, "financial", "", "", "", "") for i in (1, 2, 3)]
    no_answer = runner.verify.score([(1,)], None, no_answer=True).to_json()
    stored: dict[str, runs.Record] = {
        "bird:1": {"score": runner.verify.score([(1,)], [(2,)]).to_json()},  # answered, wrong
        "bird:2": {"score": no_answer},  # rate-limited last time
    }
    assert [q.id for q in runner.control_todo(qs, stored)] == [2, 3]
