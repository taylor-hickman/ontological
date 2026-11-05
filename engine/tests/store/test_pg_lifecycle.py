"""Postgres lifecycle tests (ARCHITECTURE.md §12): first launch, normal stop, kill -9 of the engine
then adopt, a crash, a stale postmaster.pid and a second engine. See §5.3 for the behaviour."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from conftest import (
    Apps,
    Engine,
    EngineFailed,
    backends,
    count,
    crash_postmaster,
    forge_pid,
    log_since,
    log_size,
    nearest,
    postmasters_for,
    run_stub,
    seed,
    sql,
)

from ontological.store import pg

EXTENSIONS = "pg_trgm,plpgsql,vector"


def extensions(lay: pg.Layout) -> str:
    return sql(lay, "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension")


def test_a_socket_path_over_103_bytes_is_refused_before_anything_is_written() -> None:
    lay = pg.Layout(Path("/tmp") / ("x" * 80) / "Ontological", Path("/nonexistent/bin"))
    with pytest.raises(pg.PgError, match="macOS allows 103") as err:
        pg.ensure_running(lay)
    assert "home folder" in str(err.value)
    assert not lay.app_data.exists()


def test_first_launch(apps: Apps) -> None:
    lay = apps.layout("first")
    e = Engine(lay, "--prepare-sql", "CREATE SCHEMA ops")
    assert e.action == "initdb+start"
    assert pg.data_major(lay) == 17
    settings = sql(
        lay,
        "SELECT string_agg(s, ' ' ORDER BY s) FROM ("
        "SELECT name || '=' || setting AS s FROM pg_settings WHERE name IN "
        "('listen_addresses','max_connections','shared_buffers','unix_socket_directories',"
        "'server_encoding') UNION ALL SELECT 'lc_collate=' || datcollate FROM pg_database "
        f"WHERE datname = '{pg.DATABASE}') t",
    )
    assert settings == (
        "lc_collate=C listen_addresses= max_connections=30 server_encoding=UTF8 "
        f"shared_buffers=16384 unix_socket_directories={lay.pgsocket}"
    )
    assert lay.pgsocket.stat().st_mode & 0o777 == 0o700
    hba = sql(
        lay,
        "SELECT string_agg(type || ':' || auth_method, ',' ORDER BY line_number) "
        "FROM pg_hba_file_rules",
    ).split(",")
    assert "local:trust" in hba
    assert all(r == "local:trust" or r.endswith(":reject") for r in hba), hba
    assert extensions(lay) == EXTENSIONS
    # prepare_store ran in the store before first launch finished
    assert sql(lay, "SELECT count(*) FROM pg_namespace WHERE nspname = 'ops'") == "1"
    assert sorted(p.name for p in lay.app_data.glob("pgdata*")) == ["pgdata"]
    seed(lay, 1, 100)
    assert nearest(lay) != ""
    e.term()


def test_engine_killed_while_preparing_the_store_leaves_no_pgdata(apps: Apps) -> None:
    lay = apps.layout("first-crash-store")
    assert run_stub(lay, "--die-at", "prepare-store") == 9
    assert not lay.pgdata.exists()
    staging = pg.read_pid_file(lay.app_data / "pgdata.initdb")
    assert staging is not None and pg.pid_alive(staging.pid)

    e = Engine(lay, "--prepare-sql", "CREATE SCHEMA ops")
    assert e.action == "initdb+start"
    assert extensions(lay) == EXTENSIONS
    assert sql(lay, "SELECT count(*) FROM pg_namespace WHERE nspname = 'ops'") == "1"
    assert not pg.pid_alive(staging.pid)
    assert sorted(p.name for p in lay.app_data.glob("pgdata*")) == ["pgdata"]
    e.term()


def test_engine_killed_right_after_the_rename_finds_a_complete_store(apps: Apps) -> None:
    lay = apps.layout("first-crash-rename")
    assert run_stub(lay, "--prepare-sql", "CREATE SCHEMA ops", "--die-at", "rename-initdb") == 9
    e = Engine(lay)
    assert e.action == "start"
    assert extensions(lay) == EXTENSIONS
    assert sql(lay, "SELECT count(*) FROM pg_namespace WHERE nspname = 'ops'") == "1"
    e.term()


def test_a_failing_prepare_store_leaves_no_pgdata(apps: Apps) -> None:
    lay = apps.layout("first-fail")
    with pytest.raises(EngineFailed, match="syntax error"):
        Engine(lay, "--prepare-sql", "CREATE SCHEMA")
    assert sorted(p.name for p in lay.app_data.glob("pgdata*")) == []
    e = Engine(lay)
    assert e.action == "initdb+start"
    e.term()


def test_normal_stop_writes_a_quit_dump_and_needs_no_recovery(apps: Apps) -> None:
    lay = apps.layout("stop")
    a = Engine(lay)
    seed(lay, 1, 500)
    out = a.term()
    dump = lay.backups / str(out["quit_dump"])
    assert dump.is_file() and dump.name.endswith("-quit.dump")
    assert list(lay.backups.glob("*.part")) == []
    listing = pg.run([lay.tool("pg_restore"), "-l", str(dump)]).stdout
    assert "TABLE DATA public items" in listing
    pg.wait_gone(a.postmaster, 30)
    assert not (lay.pgdata / "postmaster.pid").exists()
    assert "database system is shut down" in lay.log.read_text()

    off = log_size(lay)
    b = Engine(lay)
    assert b.action == "start"
    log = log_since(lay, off)
    assert "was interrupted" not in log and "redo starts" not in log
    assert count(lay) == 500
    b.term()


def test_kill9_of_the_engine_then_the_next_engine_adopts(apps: Apps) -> None:
    lay = apps.layout("adopt")
    a = Engine(lay)
    seed(lay, 1, 500)
    a.kill9()
    assert pg.pid_alive(a.postmaster), "pg_ctl puts the postmaster in its own session"

    off = log_size(lay)
    b = Engine(lay)
    assert b.action == "adopt"
    assert b.postmaster == a.postmaster
    assert float(b.info["seconds"]) < 1
    assert "database system is ready" not in log_since(lay, off)
    assert count(lay) == 500
    b.term()
    assert not (lay.pgdata / "postmaster.pid").exists(), "the adopting engine stops it cleanly"


def test_a_full_server_is_ours_not_stale(apps: Apps) -> None:
    """With every connection slot taken, psql gets "too many clients". That can come from our own
    healthy server, so the engine must fail and leave the lock files alone."""
    lay = apps.layout("full")
    a = Engine(lay)
    seed(lay, 1, 10)
    a.kill9()
    cmd = [lay.tool("psql"), "-X", "-h", str(lay.pgsocket), "-p", str(pg.PORT), "-U", pg.USER]
    sleepers = [
        subprocess.Popen(
            [*cmd, "-d", pg.DATABASE, "-c", "SELECT pg_sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(30)
    ]
    try:
        for _ in range(100):
            if len(backends(a.postmaster)) >= 30:
                break
            time.sleep(0.1)
        full = pg.run([*cmd, "-d", "postgres", "-c", "SELECT 1"], check=False)
        assert "too many clients" in full.stderr

        with pytest.raises(EngineFailed, match="too many clients"):
            Engine(lay)
        assert pg.pid_alive(a.postmaster)
        assert (lay.pgdata / "postmaster.pid").exists()
        assert (lay.pgsocket / f".s.PGSQL.{pg.PORT}.lock").exists()
    finally:
        # A backend in pg_sleep doesn't notice its client went away, so end the backends too.
        for p in sleepers:
            p.kill()
            p.wait()
        for pid in backends(a.postmaster):
            os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not backends(a.postmaster):
                break
            time.sleep(0.1)
    c = Engine(lay)
    assert c.action == "adopt" and c.postmaster == a.postmaster
    assert count(lay) == 10
    c.term()


def test_a_crash_of_engine_and_server_replays_wal(apps: Apps) -> None:
    lay = apps.layout("crash")
    a = Engine(lay)
    seed(lay, 1, 500)
    a.kill9()
    crash_postmaster(a.postmaster)
    assert (lay.pgdata / "postmaster.pid").exists()

    off = log_size(lay)
    b = Engine(lay)
    assert b.action == "start" and b.postmaster != a.postmaster
    log = log_since(lay, off)
    assert "was interrupted" in log and "redo done" in log
    assert count(lay) == 500
    b.term()


def _crashed_store(apps: Apps, name: str) -> pg.Layout:
    lay = apps.layout(name)
    a = Engine(lay)
    seed(lay, 1, 500)
    a.kill9()
    crash_postmaster(a.postmaster)
    return lay


def test_a_stale_pid_that_now_belongs_to_another_process(apps: Apps) -> None:
    lay = _crashed_store(apps, "stale")
    decoy = apps.decoy()
    forge_pid(lay, decoy.pid)
    status = pg.run([lay.tool("pg_ctl"), "status", "-D", str(lay.pgdata)], check=False)
    assert "server is running" in status.stdout, "pg_ctl status only checks the pid"

    b = Engine(lay)
    assert b.action == "start"
    assert "not postgres" in b.notes
    assert decoy.poll() is None
    assert count(lay) == 500
    b.term()


def test_a_stale_socket_lock_alone_is_cleared(apps: Apps) -> None:
    """Postgres refuses to start while the socket lock names a live pid, even with
    postmaster.pid gone."""
    lay = _crashed_store(apps, "stale-socket")
    decoy = apps.decoy()
    forge_pid(lay, decoy.pid)
    (lay.pgdata / "postmaster.pid").unlink()
    b = Engine(lay)
    assert b.action == "start"
    assert decoy.poll() is None
    assert count(lay) == 500
    b.term()


def test_a_stale_pid_that_is_another_clusters_postmaster(apps: Apps) -> None:
    other = apps.layout("other")
    o = Engine(other)
    lay = _crashed_store(apps, "stale-other")
    forge_pid(lay, o.postmaster)
    d = Engine(lay)
    assert d.action == "start" and d.postmaster != o.postmaster
    assert "not listening on our socket" in d.notes
    assert pg.pid_alive(o.postmaster)
    assert count(lay) == 500
    d.term()
    o.term()


def test_a_second_engine_adopts_the_first_engines_server(apps: Apps) -> None:
    lay = apps.layout("second")
    a = Engine(lay)
    b = Engine(lay)
    assert b.action == "adopt" and b.postmaster == a.postmaster
    assert len(postmasters_for(lay)) == 1
    b.kill9()
    assert pg.pid_alive(a.postmaster)
    a.term()


def test_three_engines_first_launching_at_once_share_one_server(apps: Apps) -> None:
    lay = apps.layout("race")
    engines = Engine.many(lay, 3)
    assert sorted(e.action for e in engines) == ["adopt", "adopt", "initdb+start"]
    assert len({e.postmaster for e in engines}) == 1
    assert len(postmasters_for(lay)) == 1
    for e in engines[1:]:
        e.kill9()
    engines[0].term()
