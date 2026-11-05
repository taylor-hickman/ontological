"""Choosing, writing and checking the frozen sets, on synthetic questions shaped like the real
data: Mini-Dev's per-database counts and mix, and WWI's 41 questions."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from ontological_eval import questions, sets
from ontological_eval.questions import WWI_DROPPED, WWI_FIXED_SQL, Question

# (database, simple, moderate, challenging), as in the 2026 Mini-Dev release.
SHAPE = [
    ("financial", 1, 1, 44),
    ("california_schools", 1, 5, 43),
    ("formula_1", 0, 1, 54),
    ("debit_card_specializing", 0, 0, 14),
    ("toxicology", 6, 25, 305),  # the other databases, which only count toward the mix
]


def bird() -> list[Question]:
    out: list[Question] = []
    for db, *counts in SHAPE:
        for difficulty, n in zip(("simple", "moderate", "challenging"), counts, strict=True):
            for _ in range(n):
                out.append(Question("bird", len(out), db, "", "", "SELECT 1", difficulty))
    return out


def wwi() -> list[Question]:
    return [Question("wwi", i, "wideworldimporters", "", "", "", "") for i in range(1, 42)]


def by_key() -> dict[sets.Key, Question]:
    return {q.key: q for q in [*wwi(), *bird()]}


def bird_part(keys: list[sets.Key]) -> list[Question]:
    qs = by_key()
    return [qs[k] for k in keys if k[0] == "bird"]


def test_apportion_follows_the_mix_by_largest_remainder() -> None:
    assert sets.apportion({"simple": 8, "moderate": 32, "challenging": 460}, 40) == {
        "simple": 1,
        "moderate": 2,
        "challenging": 37,
    }
    assert sum(sets.apportion({"a": 1, "b": 1, "c": 1}, 10).values()) == 10


def test_spread_gives_a_short_database_what_it_has_and_the_rest_to_the_others() -> None:
    assert sets.spread(40, {"a": 36, "b": 39, "c": 45, "d": 4}) == {
        "a": 12,
        "b": 12,
        "c": 12,
        "d": 4,
    }
    assert sets.spread(10, dict.fromkeys("abcd", 10)) == {"a": 3, "b": 3, "c": 2, "d": 2}
    with pytest.raises(sets.SetsError):
        sets.spread(5, {"a": 2, "b": 2})


def test_dev_is_every_kept_wwi_question_and_ten_per_bird_database() -> None:
    chosen = sets.choose(wwi(), bird())
    dev_wwi = [i for s, i in chosen["dev"] if s == "wwi"]
    assert dev_wwi == [i for i in range(1, 42) if i not in WWI_DROPPED]
    assert Counter(q.db for q in bird_part(chosen["dev"])) == dict.fromkeys(
        questions.BIRD_DATABASES, 10
    )


def test_heldout_is_forty_more_bird_questions_with_debit_card_giving_what_is_left() -> None:
    chosen = sets.choose(wwi(), bird())
    assert not set(chosen["dev"]) & set(chosen["heldout"])
    assert Counter(q.db for q in bird_part(chosen["heldout"])) == {
        "financial": 12,
        "california_schools": 12,
        "formula_1": 12,
        "debit_card_specializing": 4,
    }


def test_smoke_is_ten_wwi_and_ten_bird_from_dev() -> None:
    chosen = sets.choose(wwi(), bird())
    assert set(chosen["smoke"]) <= set(chosen["dev"])
    assert Counter(s for s, _ in chosen["smoke"]) == {"wwi": 10, "bird": 10}
    assert len({q.db for q in bird_part(chosen["smoke"])}) == 4


def test_bird_draws_follow_mini_devs_mix_over_all_its_databases() -> None:
    chosen = sets.choose(wwi(), bird())
    for name in ("dev", "heldout"):
        mix = Counter(q.difficulty for q in bird_part(chosen[name]))
        assert mix == {"simple": 1, "moderate": 2, "challenging": 37}, name


def test_the_seed_fixes_the_choice() -> None:
    assert sets.choose(wwi(), bird()) == sets.choose(wwi(), bird())
    assert sets.choose(wwi(), bird(), seed=1) != sets.choose(wwi(), bird(), seed=2)


def test_the_files_hold_ids_by_source_and_are_not_overwritten(tmp_path: Path) -> None:
    chosen = sets.choose(wwi(), bird())
    sets.write(chosen, tmp_path)
    assert set(json.loads((tmp_path / "smoke.json").read_text())) == {"wwi", "bird"}
    assert sets.read(tmp_path) == chosen
    with pytest.raises(sets.SetsError, match="frozen"):
        sets.write(chosen, tmp_path)
    sets.write(chosen, tmp_path, force=True)


def test_shape_problems_finds_overlap_strays_dropped_and_unknown_ids() -> None:
    good = sets.choose(wwi(), bird())
    assert sets.shape_problems(good, by_key()) == []
    bad: sets.Sets = {
        "dev": [*good["dev"], ("wwi", 1), ("bird", 9999)],
        "heldout": [*good["heldout"], good["dev"][0]],
        "smoke": [*good["smoke"], good["heldout"][0]],
    }
    problems = "\n".join(sets.shape_problems(bad, by_key()))
    assert "wwi 1 was dropped" in problems
    assert "bird 9999 isn't in the downloaded questions" in problems
    assert "dev and heldout share" in problems
    assert "smoke has questions that aren't in dev" in problems


def test_gold_queries_must_run_and_return_a_row_that_is_not_all_null(tmp_path: Path) -> None:
    (tmp_path / "bird").mkdir()
    with sqlite3.connect(tmp_path / "bird" / "financial.sqlite") as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1), (NULL)")
    runner = sets.GoldRunner(tmp_path)

    def problem(sql: str) -> str | None:
        return sets.gold_problem(Question("bird", 7, "financial", "", "", sql, "simple"), runner)

    try:
        assert problem("SELECT x FROM t") is None
        assert problem("SELECT x FROM t WHERE x > 5") == "bird 7: returns no rows"
        assert problem("SELECT max(x) FROM t WHERE x > 5") == "bird 7: returns only NULLs"
        assert (problem("SELECT nope FROM t") or "").startswith("bird 7: fails: no such column")
        assert (problem("DELETE FROM t") or "").startswith("bird 7: fails:")  # read-only
    finally:
        runner.close()


def test_wwi_gold_sql_is_fixed_on_load(tmp_path: Path) -> None:
    (tmp_path / "wwi").mkdir()
    raw = [{"question_id": i, "db_id": "WWI", "question": "", "SQL": "broken"} for i in (2, 4, 27)]
    (tmp_path / "wwi" / "evaluation.json").write_text(json.dumps(raw))
    got = {q.id: q.sql for q in questions.load_wwi(tmp_path)}
    assert got == {2: "broken", 4: WWI_FIXED_SQL[4], 27: WWI_FIXED_SQL[27]}
