"""The retrieval benchmark (ARCHITECTURE.md §7.4, BUILD_PLAN.md M3.9).

Gold extraction, recall and the threshold's calibration run on their own. The end-to-end test
ingests two small BIRD-shaped SQLite files into an eval store and runs the data layer with the
default model: it needs ONTOLOGICAL_PG_BIN and ONTOLOGICAL_MODEL_DIR, and skips without either
(CI sets both).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from ontological.catalog.parsers.catalog_index import CatalogIndex, SchemaEntry, TableEntry
from ontological.embed import model as models
from ontological.store import dal, pg

from ontological_eval import ingest, retrieval
from ontological_eval.home import app_data_dir, bird_dir
from ontological_eval.questions import Question


def q(sql: str, *, db: str = "financial", source: str = "bird", qid: int = 1) -> Question:
    return Question(source, qid, db, "a question", "", sql, "")  # pyright: ignore[reportArgumentType]


def index() -> CatalogIndex:
    account = TableEntry("t-account", "account")
    account.columns.update({"account_id": "c-account-id", "district_id": "c-account-district"})
    district = TableEntry("t-district", "district")
    district.columns.update({"district_id": "c-district-id", "a2": "c-a2", "a4": "c-a4"})
    loan = TableEntry("t-loan", "loan")
    loan.columns.update({"account_id": "c-loan-account", "amount": "c-amount"})
    main = SchemaEntry()
    main.tables.update({"account": account, "district": district, "loan": loan})
    return CatalogIndex({"main": main})


def test_gold_resolves_aliases_and_ctes_to_catalog_ids() -> None:
    g = retrieval.gold(
        q(
            "WITH big AS (SELECT account_id, SUM(amount) AS total FROM loan GROUP BY account_id) "
            "SELECT d.A2, b.total FROM account a JOIN district d ON d.district_id = a.district_id "
            "JOIN big b ON b.account_id = a.account_id"
        ),
        index(),
    )
    assert g.tables == {"t-account", "t-district", "t-loan"}
    assert {"c-a2", "c-amount", "c-account-district", "c-district-id"} <= g.columns
    assert "c-a4" not in g.columns


def test_gold_sql_that_names_no_catalog_table_is_an_error() -> None:
    with pytest.raises(retrieval.BenchmarkError, match="names no table"):
        retrieval.gold(q("SELECT 1 FROM elsewhere"), index())


def test_column_tables_maps_each_column_to_its_table() -> None:
    tables_of = retrieval.column_tables(index())
    assert tables_of["c-a2"] == "t-district"
    assert tables_of["c-amount"] == "t-loan"


def test_recall_is_the_fraction_found_and_none_when_nothing_is_wanted() -> None:
    assert retrieval.recall(["a", "x"], frozenset({"a", "b"})) == 0.5
    assert retrieval.recall([], frozenset()) is None


def test_calibrate_puts_the_threshold_between_separable_distances() -> None:
    t = retrieval.calibrate([0.2, 0.25, 0.3], [0.5, 0.6, 0.8])
    assert 0.3 < t.value < 0.5
    assert (t.tpr, t.fpr) == (1.0, 0.0)
    assert (t.upstream_tpr, t.upstream_fpr) == (1.0, 2 / 3)  # 0.75 lets two negatives through


def test_calibrate_trades_off_overlapping_distances() -> None:
    t = retrieval.calibrate([0.2, 0.3, 0.55], [0.4, 0.5, 0.6, 0.7])
    assert 0.3 < t.value < 0.4  # catching 0.55 would let 0.4 and 0.5 in too
    assert t.tpr == pytest.approx(2 / 3)
    assert t.fpr == 0.0


def test_calibrate_needs_both_sides() -> None:
    with pytest.raises(retrieval.BenchmarkError):
        retrieval.calibrate([0.2], [])


# --- end to end ---------------------------------------------------------------------------------

SCHOOLS = """
    CREATE TABLE schools (CDSCode TEXT PRIMARY KEY, County TEXT, OpenDate DATE);
    CREATE TABLE satscores (cds TEXT REFERENCES schools, NumTstTakr INTEGER);
    INSERT INTO schools VALUES ('01', 'Los Angeles', '2007-09-01'), ('02', 'Alameda', '1990-01-01');
    INSERT INTO satscores VALUES ('01', 40), ('02', 12);
"""
FINANCIAL = """
    CREATE TABLE district (district_id INTEGER PRIMARY KEY, A2 TEXT, A4 INTEGER);
    CREATE TABLE loan (loan_id INTEGER PRIMARY KEY, district_id INTEGER, amount INTEGER);
    INSERT INTO district VALUES (1, 'Prachatice', 51428), (2, 'Praha', 1204953);
    INSERT INTO loan VALUES (5, 1, 96396), (6, 2, 80952);
"""


@pytest.fixture
def default_model_dir() -> Path:
    value = os.environ.get("ONTOLOGICAL_MODEL_DIR")
    if not value:
        if os.environ.get("ONTOLOGICAL_MODEL_REQUIRED") == "1":
            pytest.fail("ONTOLOGICAL_MODEL_DIR is not set and ONTOLOGICAL_MODEL_REQUIRED=1")
        pytest.skip("set ONTOLOGICAL_MODEL_DIR to a directory holding the default model")
    return Path(value)


def test_the_data_layer_benchmark_scores_every_configuration(
    home: Path, pg_bin: Path, default_model_dir: Path
) -> None:
    for name, script in (("california_schools", SCHOOLS), ("financial", FINANCIAL)):
        path = bird_dir(home) / f"{name}.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        try:
            con.executescript(script)
        finally:
            con.close()
    ingest.run(home, pg_bin, ["california_schools", "financial"])
    questions = [
        Question(
            "bird",
            1,
            "california_schools",
            "How many SAT test takers did Los Angeles County schools have?",
            "",
            "SELECT SUM(s.NumTstTakr) FROM satscores s JOIN schools c ON c.CDSCode = s.cds "
            "WHERE c.County = 'Los Angeles'",
            "simple",
        ),
        Question(
            "bird",
            2,
            "financial",
            "What is the total loan amount in the Prachatice district?",
            "A2 is the district name.",
            "SELECT SUM(l.amount) FROM loan l JOIN district d ON d.district_id = l.district_id "
            "WHERE d.A2 = 'Prachatice'",
            "simple",
        ),
    ]

    lay = pg.Layout(app_data_dir(home), pg_bin)
    pg.ensure_running(lay)
    dal.connect(lay)
    try:
        report = retrieval.run(
            "data", questions, [models.DEFAULT], lambda _: default_model_dir, ks=(1, 20)
        )
    finally:
        dal.close()

    [result] = report.models
    assert result.model == models.DEFAULT.key
    assert result.rows == 4 + 11  # 4 tables and 11 columns
    assert set(result.recall) == set(retrieval.CONFIGS)
    for by_k in result.recall.values():
        for scores in by_k.values():
            assert 0.0 <= scores["tables"] <= 1.0
            assert 0.0 <= scores["columns"] <= 1.0
    # At k = 20 every column of these tiny databases is in the pack.
    assert result.recall["dense"][20] == {"tables": 1.0, "columns": 1.0}
    assert result.recall["hybrid"][20] == {"tables": 1.0, "columns": 1.0}
    assert result.threshold is not None
    assert len(result.threshold.positives) == 2
    assert len(result.threshold.negatives) == 2
    assert "| arctic-m | hybrid |" in retrieval.markdown(report)
