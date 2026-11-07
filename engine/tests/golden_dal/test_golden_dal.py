"""Upstream's golden DAL suite: M2's conformance gate (ARCHITECTURE.md §5.4, §11; M2.1).

The gate brings up a store on the bundled binaries the way the engine does (`pg.ensure_running`:
initdb, both Alembic chains, the socket only). It loads upstream's Pagila (a Postgres database in
the same cluster) and Chinook (a SQLite file) fixtures, and seeds the golden graph through
upstream's own connectors and catalog ingest. Then it runs upstream's DAL and catalog-store tests in
a pytest of their own. All of that upstream code is vendored as test-only code under
tests/upstream_fixtures/ (UPSTREAM.md there).

A skip is a failure. With `POSTGRES_*` unset or wrong, most of upstream's suite skips and pytest
still exits 0 (spike S6), so the gate reads the JUnit report: every test passes, and every file runs
exactly the tests it has.

    ONTOLOGICAL_PG_BIN=<trimmed 17 tree>/bin uv run pytest tests/golden_dal

Skips without ONTOLOGICAL_PG_BIN unless ONTOLOGICAL_PG_REQUIRED=1, as the store tests do.
ONTOLOGICAL_GOLDEN_REPORTS names a directory to keep the seed's log and the suite's output and
JUnit XML in; otherwise they go with the run's temporary directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alembic import command

from ontological.store import chains, dal, pg

FIXTURES = Path(__file__).parents[1] / "upstream_fixtures"
SUITE = ("auto_ontology/dal/tests", "auto_ontology/catalog/store/tests")
PAGILA = "pagila"
# build_sqlite_fixtures writes it beside its SQL, as upstream's does; .gitignore'd.
CHINOOK = FIXTURES / "dev_tools/fixtures/sql/chinook.sqlite"

# Every test file in the suite and how many tests it runs: S6's 781 upstream DAL tests, less
# test_migration_revision.py's Helm-chart test, and the 23 catalog-store tests, 803 in all.
# test_model_interchange.py runs against M6.4's port of dal/model_interchange.py. A file that runs
# fewer tests than this fails the gate.
EXPECTED = {
    "auto_ontology/catalog/store/tests/test_primary_keys.py": 3,
    "auto_ontology/catalog/store/tests/test_review_regressions.py": 6,
    "auto_ontology/catalog/store/tests/test_rows_and_links.py": 14,
    "auto_ontology/dal/tests/test_analyses.py": 27,
    "auto_ontology/dal/tests/test_attributes.py": 60,
    "auto_ontology/dal/tests/test_candidates.py": 16,
    "auto_ontology/dal/tests/test_close_store.py": 2,
    "auto_ontology/dal/tests/test_column_properties.py": 9,
    "auto_ontology/dal/tests/test_dal_surface.py": 25,
    "auto_ontology/dal/tests/test_datasources_semantic.py": 37,
    "auto_ontology/dal/tests/test_exploration.py": 33,
    "auto_ontology/dal/tests/test_golden.py": 128,
    "auto_ontology/dal/tests/test_groups.py": 7,
    "auto_ontology/dal/tests/test_metadata_batch.py": 8,
    "auto_ontology/dal/tests/test_migration_revision.py": 1,
    "auto_ontology/dal/tests/test_model_interchange.py": 21,
    "auto_ontology/dal/tests/test_reset.py": 14,
    "auto_ontology/dal/tests/test_rules.py": 35,
    "auto_ontology/dal/tests/test_schema_version.py": 9,
    "auto_ontology/dal/tests/test_search.py": 69,
    "auto_ontology/dal/tests/test_session.py": 10,
    "auto_ontology/dal/tests/test_sql_attributes.py": 35,
    "auto_ontology/dal/tests/test_sql_fragments.py": 12,
    "auto_ontology/dal/tests/test_tags.py": 143,
    "auto_ontology/dal/tests/test_terms.py": 52,
    "auto_ontology/dal/tests/test_users.py": 12,
    "auto_ontology/dal/tests/test_zones.py": 15,
}


# --- the JUnit report --------------------------------------------------------------------------


@dataclass
class Report:
    """Per file, how many tests passed; and every test that didn't, with why."""

    passed: Counter[str] = field(default_factory=Counter[str])
    ran: Counter[str] = field(default_factory=Counter[str])
    not_passed: list[str] = field(default_factory=list[str])


def _file_of(case: ET.Element) -> str:
    """The test's file. A collection error has no `file`, only the module's dotted name."""
    if file := case.get("file"):
        return file
    dotted = (case.get("classname") or case.get("name") or "?").split(".")
    at = next((i for i, part in enumerate(dotted) if part.startswith("test_")), len(dotted) - 1)
    return "/".join(dotted[: at + 1]) + ".py"


def read_report(xml: Path) -> Report:
    """Read pytest's JUnit XML (junit_family=xunit1, which names each test's file). A skip, an
    xfail or an error counts against a test, as a failure does."""
    report = Report()
    for case in ET.parse(xml).iter("testcase"):
        key = _file_of(case)
        report.ran[key] += 1
        bad = next(
            (case.find(t) for t in ("failure", "error", "skipped") if case.find(t) is not None),
            None,
        )
        if bad is None:
            report.passed[key] += 1
            continue
        message = (bad.get("message") or bad.text or "").strip().splitlines()
        report.not_passed.append(
            f"{bad.tag}: {key}::{case.get('name')}: {message[0][:200] if message else ''}"
        )
    return report


def test_a_skip_counts_against_a_test(tmp_path: Path) -> None:
    xml = tmp_path / "r.xml"
    xml.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="test_a" name="test_ok" file="dal/tests/test_a.py"/>'
        '<testcase classname="test_a" name="test_skip" file="dal/tests/test_a.py">'
        '<skipped type="pytest.skip" message="POSTGRES_* not set"/></testcase>'
        '<testcase classname="test_b" name="test_err" file="dal/tests/test_b.py">'
        '<error message="schema unavailable"/></testcase>'
        '<testcase classname="" name="dal.tests.test_c"><error message="collection failure"/>'
        "</testcase></testsuite></testsuites>"
    )
    report = read_report(xml)
    assert report.ran == {
        "dal/tests/test_a.py": 2,
        "dal/tests/test_b.py": 1,
        "dal/tests/test_c.py": 1,
    }
    assert report.passed == {"dal/tests/test_a.py": 1}
    assert report.not_passed == [
        "skipped: dal/tests/test_a.py::test_skip: POSTGRES_* not set",
        "error: dal/tests/test_b.py::test_err: schema unavailable",
        "error: dal/tests/test_c.py::dal.tests.test_c: collection failure",
    ]


# --- the run -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bin17() -> Path:
    value = os.environ.get("ONTOLOGICAL_PG_BIN")
    if not value:
        if os.environ.get("ONTOLOGICAL_PG_REQUIRED") == "1":
            pytest.fail("ONTOLOGICAL_PG_BIN is not set and ONTOLOGICAL_PG_REQUIRED=1")
        pytest.skip("set ONTOLOGICAL_PG_BIN to a PostgreSQL 17 bin/ directory")
    return Path(value).resolve()


def _run(args: list[str], env: dict[str, str], log: Path) -> None:
    """Run a step of the gate in the fixture tree, failing with its output if it fails."""
    with log.open("a") as out:
        out.write(f"\n$ {' '.join(args)}\n")
        out.flush()
        done = subprocess.run(args, cwd=FIXTURES, env=env, stdout=out, stderr=subprocess.STDOUT)
    if done.returncode != 0:
        pytest.fail(f"{' '.join(args[:4])} exited {done.returncode}:\n{log.read_text()[-4000:]}")


@pytest.fixture(scope="module")
def suite(bin17: Path) -> Iterator[Report]:
    """The seeded store, and the suite's report against it."""
    # Under /tmp: the socket path has to fit in 103 bytes.
    root = Path(tempfile.mkdtemp(prefix="golden-", dir="/tmp"))
    layout = pg.Layout(root / "Ontological", bin17)
    reports = Path(os.environ.get("ONTOLOGICAL_GOLDEN_REPORTS") or root / "reports")
    reports.mkdir(parents=True, exist_ok=True)
    log = reports / "golden.log"
    try:
        pg.ensure_running(layout)
        pg.psql(layout, f"CREATE DATABASE {PAGILA}", db="postgres")
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("POSTGRES_")},
            **dal.environ(layout),
            # Only seed_local_postgres reads it; the socket needs none.
            "POSTGRES_PASSWORD": "",
            # Through upstream's own connectors, which take a plain postgresql:// URL.
            "CONNECTION_STRINGS": (
                pg.url(layout, PAGILA).replace("+psycopg", "", 1) + f",sqlite:///{CHINOOK}"
            ),
        }
        env.pop("DATABASE_URL", None)
        # Upstream's CI job's steps, after `alembic upgrade head` (ensure_running ran it).
        _run([sys.executable, "-m", "dev_tools.fixtures.seed_fixtures"], env, log)
        _run([sys.executable, "-m", "seed_graph", "--reset"], env, log)
        # `alembic check`: upstream's migrations still describe its models.
        command.check(chains.config(chains.UPSTREAM, pg.url(layout)))
        xml, out = reports / "golden.xml", reports / "golden.out"
        pytest_args = [*SUITE, f"--junitxml={xml}", "-o", "junit_family=xunit1"]
        with out.open("w") as f:
            subprocess.run(
                [sys.executable, "-m", "pytest", *pytest_args],
                cwd=FIXTURES,
                env=env,
                stdout=f,
                stderr=subprocess.STDOUT,
            )
        assert xml.exists(), out.read_text()[-4000:]
        yield read_report(xml)
    finally:
        if layout.pgdata.exists():
            pg.stop(layout, quit_dump=False)
        shutil.rmtree(root, ignore_errors=True)


def test_every_test_passes_and_none_skip(suite: Report) -> None:
    assert suite.not_passed == []


def test_every_file_runs_all_its_tests(suite: Report) -> None:
    assert dict(suite.ran) == EXPECTED
