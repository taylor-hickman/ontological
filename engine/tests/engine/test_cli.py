"""`ontological ask` (BUILD_PLAN.md M1.8): what it prints, and the real command against
serve_stub.py, the real engine with a fake store whose answers are `fake_answers.py`'s. The engine
starts headless, so its lease shows in logs/engine.log as `taken by ontological ask`."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from engine_proc import STUB, app_data
from fake_answers import SQL, FakeAnswers

from ontological.answer.models import AnswerModel
from ontological.answer.service import Asked, Ran
from ontological.cli import main, render

__all__ = ["app_data"]  # the fixture

FAST = ("--idle-timeout", "1", "--lease-renew", "0.25")
ENGINE = (sys.executable, str(STUB))


def fake(*, critic: bool = True) -> Asked:
    return asyncio.run(FakeAnswers().ask("Who has the most albums?", "chinook", critic=critic))


def test_render_prints_the_steps_the_sql_the_rows_and_the_verdict() -> None:
    out = render(fake())
    lines = out.splitlines()

    assert lines[0] == "Steps"
    assert lines[1].startswith('  1. plan {"intent": "calculation"')
    assert lines[2] == '  2. validate_sql {"sql": "SELECT * FROM Albums"}'
    assert lines[3] == "     error: Albums isn't a table in chinook. Did you mean Album?"
    assert "  5. review_answer" in out
    assert f"SQL (validated)\n  {SQL}\n" in out
    assert "Rows (2)\n  Name          albums\n  Iron Maiden   21\n  Led Zeppelin  14\n" in out
    assert "Answer\n  Iron Maiden has the most albums" in out
    assert lines[-2:] == ["Verified", "Model: claude-opus-5-5"]


def test_render_says_why_an_answer_is_unverified() -> None:
    out = render(fake(critic=False))
    assert "5. review_answer" not in out
    assert "Unverified: review_answer never ran val-1" in out


def test_render_marks_sql_that_never_passed_and_a_missing_answer() -> None:
    unvalidated = fake().model_copy(update={"sql": None, "result": None, "verified": False})
    out = render(unvalidated)
    assert f"SQL (not validated)\n  {SQL}" in out
    assert "Rows" not in out

    nothing = Asked(
        question="q",
        database="chinook",
        answer=None,
        verified=False,
        reason="the session ended with no answer",
        sql=None,
        result=None,
        steps=[],
        outcome="invalid",
        error="out of turns",
        answered_model=None,
    )
    out = render(nothing)
    assert "Steps\n  none" in out and "SQL: none" in out and "Answer\n  none" in out
    assert "Unverified: the session ended with no answer\nSession invalid: out of turns" in out


def test_render_clips_long_cells_and_counts_rows_past_the_cap() -> None:
    ran = Ran(
        columns=["n", "note"],
        types=["INTEGER", "VARCHAR"],
        rows=[[i, "x" * 60 if i == 0 else None] for i in range(60)],
        truncated=True,
    )
    out = render(fake().model_copy(update={"result": ran}))
    assert "Rows (60+)" in out
    assert "x" * 39 + "…" in out and "x" * 41 not in out
    assert "NULL" in out
    assert "  … 10 more" in out
    assert "(the source returned only these; the query has more)" in out


def test_render_says_when_the_question_isnt_answerable() -> None:
    answer = AnswerModel(
        answer="Ontological doesn't do predictions.",
        plan_id="plan-1",
        answerable=False,
        missing=["prediction_unsupported"],
        assumptions=["next year means 2027"],
    )
    out = render(fake().model_copy(update={"answer": answer, "sql": None, "result": None}))
    assert "  assumed: next year means 2027" in out
    assert "  not answerable; missing: prediction_unsupported" in out
    assert "SQL: none" in out


# --- the command against an engine -------------------------------------------------------------


def engine_log(app_data: Path) -> str:
    path = app_data / "logs" / "engine.log"
    return path.read_text() if path.exists() else ""


def ask(app_data: Path, *args: str) -> int:
    return main(["ask", *args, "--app-data", str(app_data), *FAST], engine=ENGINE)


def test_ask_starts_an_engine_holds_a_lease_and_prints_the_answer(
    app_data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ask(app_data, "Who has the most albums?", "--source", "chinook")
    out, err = capsys.readouterr()

    assert code == 0, err
    assert out.startswith("Steps\n  1. plan ")
    assert f"SQL (validated)\n  {SQL}" in out
    assert "Iron Maiden   21" in out
    assert out.rstrip().endswith("Model: claude-opus-5-5")
    assert "Asking chinook…" in err
    log = engine_log(app_data)
    assert log.count("taken by ontological ask") == 1
    assert log.count(" released") == 1, "the lease is released once the answer is in"


def test_an_unverified_answer_exits_2(app_data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ask(app_data, "q", "--source", "chinook", "--no-critic") == 2
    assert "Unverified: review_answer never ran val-1" in capsys.readouterr().out


def test_a_database_with_no_source_exits_1_with_the_engines_reason(
    app_data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ask(app_data, "q", "--source", "nope") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "ontological ask: the engine answered 404: no source for nope" in err


def test_no_engine_exits_1(app_data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app_data.mkdir(parents=True)
    (app_data / "store.log").mkdir()  # FakeStore can't append to a directory, so it can't start
    assert ask(app_data, "q", "--source", "chinook") == 1
    assert "ontological ask: no engine:" in capsys.readouterr().err


def test_ask_needs_a_question_and_a_source(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["ask", "q"])
    assert exited.value.code == 2
    assert "--source" in capsys.readouterr().err
