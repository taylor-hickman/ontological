"""Major-version restore (ARCHITECTURE.md §5.3, Major-version upgrades; §12): a store made by the
previous release on PostgreSQL 16, moved to 17 by restoring its dump into a new cluster. pgdata/
must never end up missing or empty, whatever step a crash or a failure lands on."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import Apps, Engine, EngineFailed, count, crash_postmaster, run_stub, seed, sql

from ontological.store import pg


def old_app(apps: Apps, bin16: Path, name: str) -> tuple[pg.Layout, pg.Layout]:
    """The previous release's layout (PostgreSQL 16) and this release's, on one <appData>."""
    old = apps.layout(name, bin16)
    return old, pg.Layout(old.app_data, apps.bin17)


def old_store(apps: Apps, bin16: Path, name: str) -> pg.Layout:
    """A 16 store with 500 rows that quit cleanly; this release's layout for it."""
    old, new = old_app(apps, bin16, name)
    a = Engine(old)
    seed(old, 1, 500)
    a.term()
    return new


def assert_moved(lay: pg.Layout, rows: int) -> None:
    assert pg.data_major(lay) == 17
    assert sorted(p.name for p in lay.app_data.glob("pgdata.*")) == []
    assert sql(lay, "SHOW server_version_num")[:2] == "17"
    assert count(lay) == rows
    assert sql(lay, "SELECT extversion FROM pg_extension WHERE extname='vector'") != ""
    plan = sql(
        lay,
        "SET enable_seqscan = off;"
        "EXPLAIN SELECT id FROM items ORDER BY embedding <=> '[0,1,3]' LIMIT 1",
    )
    assert "items_hnsw" in plan
    asides = list(lay.app_data.glob("pgdata-16-*"))
    assert len(asides) == 1
    assert (asides[0] / "PG_VERSION").read_text().strip() == "16"


def test_a_clean_quit_dump_moves_to_17(apps: Apps, bin16: Path) -> None:
    old, new = old_app(apps, bin16, "major")
    a = Engine(old, "--prepare-sql", "CREATE SCHEMA app")
    assert pg.data_major(old) == 16
    seed(old, 1, 500)
    assert str(a.term()["quit_dump"]).endswith("-quit.dump")

    b = Engine(new)
    assert b.action == "upgrade"
    assert "stale" not in b.notes
    assert_moved(new, 500)
    assert sql(new, "SELECT count(*) FROM pg_namespace WHERE nspname = 'app'") == "1"
    b.term()
    c = Engine(new)
    assert c.action == "start"
    c.term()


def test_a_stale_dump_is_reported_then_restored(apps: Apps, bin16: Path) -> None:
    old, new = old_app(apps, bin16, "major-stale")
    a = Engine(old)
    seed(old, 1, 500)
    a.term()
    a = Engine(old)
    seed(old, 501, 100)
    a.kill9()
    crash_postmaster(a.postmaster)

    b = Engine(new)
    assert "stale dump" in b.notes and "did not stop cleanly" in b.notes
    assert_moved(new, 500)
    b.term()


def test_a_running_16_server_is_dumped_live(apps: Apps, bin16: Path) -> None:
    old, new = old_app(apps, bin16, "major-live")
    a = Engine(old)
    seed(old, 1, 500)
    a.term()
    a = Engine(old)
    seed(old, 501, 100)
    a.kill9()
    assert pg.pid_alive(a.postmaster)

    b = Engine(new)
    assert "dumped the running PostgreSQL 16" in b.notes
    assert not pg.pid_alive(a.postmaster)
    assert_moved(new, 600)
    b.term()


def test_engine_killed_during_the_restore_redoes_the_move(apps: Apps, bin16: Path) -> None:
    new = old_store(apps, bin16, "major-kill-restore")
    assert run_stub(new, "--die-at", "pg_restore") == 9
    assert pg.data_major(new) == 16

    b = Engine(new)
    assert "discarded an unfinished" in b.notes
    assert_moved(new, 500)
    b.term()


def test_engine_killed_mid_swap_finishes_the_swap(apps: Apps, bin16: Path) -> None:
    new = old_store(apps, bin16, "major-kill-swap")
    assert run_stub(new, "--die-at", "aside") == 9
    assert not new.pgdata.exists()

    b = Engine(new)
    assert b.action == "start", "never a first launch onto an empty store"
    assert "finished an interrupted" in b.notes
    assert_moved(new, 500)
    b.term()


def test_a_failed_restore_leaves_the_16_cluster_in_place(apps: Apps, bin16: Path) -> None:
    new = old_store(apps, bin16, "major-bad-dump")
    good = pg.newest_dump(new)
    assert good is not None
    bad = good.with_name(good.name.replace("-quit", "9-quit"))  # sorts after the good one
    bad.write_bytes(good.read_bytes()[: good.stat().st_size // 2])

    with pytest.raises(EngineFailed, match="pg_restore"):
        Engine(new)
    assert pg.data_major(new) == 16
    assert sorted(p.name for p in new.app_data.glob("pgdata*")) == ["pgdata"]

    bad.unlink()
    b = Engine(new)
    assert_moved(new, 500)
    b.term()
