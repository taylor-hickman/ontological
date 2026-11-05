"""The engine on the bundled Postgres: it starts the store before it serves, and an idle shutdown
writes the quit dump and stops the server (ARCHITECTURE.md §4.4, §5.3).

Needs ONTOLOGICAL_PG_BIN, as the store tests do (see tests/store/conftest.py); skips without it
unless ONTOLOGICAL_PG_REQUIRED=1.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from pathlib import Path

import pytest
from engine_proc import Proc, app_data, http

from ontological.store import pg

__all__ = ["app_data"]  # the fixture


@pytest.fixture
def pg_bin() -> Path:
    value = os.environ.get("ONTOLOGICAL_PG_BIN")
    if not value:
        if os.environ.get("ONTOLOGICAL_PG_REQUIRED") == "1":
            pytest.fail("ONTOLOGICAL_PG_BIN is not set and ONTOLOGICAL_PG_REQUIRED=1")
        pytest.skip("set ONTOLOGICAL_PG_BIN to a PostgreSQL 17 bin/ directory")
    return Path(value).resolve()


def test_the_engine_runs_the_store_and_idles_out_with_a_quit_dump(
    app_data: Path, pg_bin: Path
) -> None:
    layout = pg.Layout(app_data, pg_bin)
    e = Proc(
        [
            sys.executable, "-m", "ontological.engine", "--app-data", str(app_data),
            "--pg-bin", str(pg_bin), "--idle-timeout", "2",
        ]
    )  # fmt: skip
    try:
        assert e.info["started"]
        state = pg.inspect_pid_file(layout)
        assert isinstance(state, pg.Running), "the store is up before the engine serves"
        postmaster = state.pid_file.pid
        assert http(app_data, "GET", "/health")[0] == 200

        assert e.wait_exit(60) == 0
        assert not pg.pid_alive(postmaster)
        assert pg.read_pid_file(layout.pgdata) is None, "a clean stop removes postmaster.pid"
        dumps = sorted(layout.backups.glob("ontology-*-quit.dump"))
        assert len(dumps) == 1 and dumps[0].stat().st_size > 0
        assert not (app_data / "engine.json").exists()
    finally:
        pf = pg.read_pid_file(layout.pgdata)
        if pf and pg.pid_alive(pf.pid) and pg.is_postgres(pf.pid):
            os.kill(pf.pid, signal.SIGQUIT)
            with contextlib.suppress(pg.PgError):
                pg.wait_gone(pf.pid, 30)
