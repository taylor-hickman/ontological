"""Migrations, dumps and restore (ARCHITECTURE.md §5.3, Dumps and Migrations; §12, "failed
migration then restore"). The Alembic chains arrive in M0.4; here each revision is plain SQL in
its own transaction, as Alembic's transaction_per_migration runs them."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from conftest import Apps, Engine, count, nearest, run_stub, seed, sql

from ontological.store import pg

REV1 = (
    "CREATE SCHEMA app; CREATE TABLE app.setting (key text PRIMARY KEY, value jsonb);"
    "INSERT INTO app.setting VALUES ('theme', '\"dark\"'); UPDATE ops.version SET rev = 1"
)
REV2 = (
    "CREATE TABLE app.conversation (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), title text);"
    "UPDATE ops.version SET rev = 2"
)
REV3 = "ALTER TABLE app.setting ADD COLUMN updated timestamptz; SELECT 1 / 0"
OPS = (
    "CREATE SCHEMA ops; CREATE TABLE ops.version (rev int NOT NULL);"
    "INSERT INTO ops.version VALUES (0)"
)


def apply(lay: pg.Layout, rev: str) -> None:
    """One revision in its own transaction, as Alembic's transaction_per_migration runs them."""
    sql(lay, f"BEGIN; {rev}; COMMIT;")


def at_revision_1(apps: Apps, name: str) -> tuple[pg.Layout, Engine]:
    lay = apps.layout(name)
    e = Engine(lay, "--prepare-sql", OPS)
    apply(lay, REV1)
    seed(lay, 1, 500)
    return lay, e


def rev(lay: pg.Layout) -> str:
    return sql(lay, "SELECT rev FROM ops.version")


def has_conversation(lay: pg.Layout) -> bool:
    return sql(lay, "SELECT to_regclass('app.conversation')") == "app.conversation"


def databases(lay: pg.Layout) -> list[str]:
    return sql(lay, "SELECT datname FROM pg_database ORDER BY datname", db="postgres").split()


def upgrade_to(revisions: list[str]) -> Callable[[pg.Layout], None]:
    def upgrade(lay: pg.Layout) -> None:
        for r in revisions[int(rev(lay)) :]:
            apply(lay, r)

    return upgrade


def pending(revisions: list[str]) -> Callable[[pg.Layout], bool]:
    return lambda lay: int(rev(lay)) < len(revisions)


def test_failed_migration_then_restore(apps: Apps) -> None:
    lay, e = at_revision_1(apps, "migrate")
    before = nearest(lay)
    # The engine's pool holds connections while migrations run.
    held = subprocess.Popen(
        [lay.tool("psql"), "-X", "-h", str(lay.pgsocket), "-p", str(pg.PORT), "-U", pg.USER,
         "-d", pg.DATABASE, "-c", "SELECT pg_sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    time.sleep(0.3)

    revisions = [REV1, REV2, REV3]
    with pytest.raises(pg.MigrationFailed, match="division by zero") as failed:
        pg.migrate(lay, pending=pending(revisions), upgrade=upgrade_to(revisions))
    dump = failed.value.restored_from
    assert dump.name.endswith("-premigrate.dump") and dump.is_file()
    held.wait(10)
    assert held.returncode != 0, "restore ends the pool's connections"
    assert rev(lay) == "1", "revision 2 had committed; the restore undid it"
    assert not has_conversation(lay)
    cols = sql(
        lay,
        "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) "
        "FROM information_schema.columns WHERE table_schema='app' AND table_name='setting'",
    )
    assert cols == "key,value"
    assert count(lay) == 500
    assert sql(lay, "SELECT value FROM app.setting") == '"dark"'
    assert nearest(lay) == before, "the HNSW index is back"
    assert pg.RESTORE_DATABASE not in databases(lay)
    e.term()


def test_a_migration_dumps_first_and_nothing_pending_does_nothing(apps: Apps) -> None:
    lay, e = at_revision_1(apps, "migrate-ok")
    revisions = [REV1, REV2]
    dump = pg.migrate(lay, pending=pending(revisions), upgrade=upgrade_to(revisions))
    assert dump is not None and dump.name.endswith("-premigrate.dump")
    assert rev(lay) == "2" and has_conversation(lay)
    assert pg.migrate(lay, pending=pending(revisions), upgrade=upgrade_to(revisions)) is None
    assert sorted(lay.backups.glob("*.dump")) == [dump]
    e.term()


def test_a_migration_whose_restore_fails_too_says_so(
    apps: Apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay, e = at_revision_1(apps, "migrate-both")
    real_run = pg.run

    def run(cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if Path(cmd[0]).name == "pg_restore":
            raise pg.PgError("pg_restore failed (1): simulated")
        return real_run(cmd, check=check)

    monkeypatch.setattr(pg, "run", run)
    revisions = [REV1, REV2, REV3]
    with pytest.raises(pg.PgError, match="so did restoring") as failed:
        pg.migrate(lay, pending=pending(revisions), upgrade=upgrade_to(revisions))
    assert not isinstance(failed.value, pg.MigrationFailed), "no restore happened"
    assert "as the failed migration left it" in str(failed.value)
    assert rev(lay) == "2" and has_conversation(lay)
    monkeypatch.undo()
    e.term()


def _ahead_of_dump(apps: Apps, name: str) -> tuple[pg.Layout, Engine, Path]:
    """A store at revision 2 and a dump of it at revision 1, so it shows whether a restore
    touched the store."""
    lay, e = at_revision_1(apps, name)
    dump = pg.dump(lay, "premigrate")
    apply(lay, REV2)
    return lay, e, dump


def untouched(lay: pg.Layout) -> bool:
    return (
        rev(lay) == "2"
        and has_conversation(lay)
        and count(lay) == 500
        and pg.RESTORE_DATABASE not in databases(lay)
    )


def test_a_truncated_dump_leaves_the_store_untouched(apps: Apps) -> None:
    lay, e, dump = _ahead_of_dump(apps, "truncated")
    bad = dump.with_name("truncated.dump")
    bad.write_bytes(dump.read_bytes()[: dump.stat().st_size // 2])
    with pytest.raises(pg.PgError, match="store is unchanged"):
        pg.restore(lay, bad)
    assert untouched(lay)
    e.term()


def test_a_missing_dump_leaves_the_store_untouched(apps: Apps) -> None:
    lay, e, dump = _ahead_of_dump(apps, "missing")
    with pytest.raises(pg.PgError, match="missing"):
        pg.restore(lay, dump.with_name("missing.dump"))
    assert untouched(lay)
    e.term()


def test_engine_killed_during_pg_restore_next_start_discards_it(apps: Apps) -> None:
    lay, e, dump = _ahead_of_dump(apps, "kill-restore")
    e.kill9()
    assert run_stub(lay, "--restore", str(dump), "--die-at", "pg_restore") == 9
    assert pg.RESTORE_DATABASE in databases(lay)
    b = Engine(lay)
    assert b.action == "adopt"
    assert "discarded an unfinished restore" in b.notes
    assert untouched(lay)
    b.term()


def test_engine_killed_between_drop_and_rename_next_start_finishes_it(apps: Apps) -> None:
    lay, e, dump = _ahead_of_dump(apps, "kill-swap")
    e.kill9()
    assert run_stub(lay, "--restore", str(dump), "--die-at", "drop-store") == 9
    assert databases(lay).count(pg.DATABASE) == 0
    b = Engine(lay)
    assert "finished an interrupted restore" in b.notes
    assert rev(lay) == "1" and not has_conversation(lay) and count(lay) == 500
    b.term()


def test_an_interrupted_restore_is_finished_when_the_server_was_down_too(apps: Apps) -> None:
    """The next start checks for leftovers after starting a stopped server, not only on adopt."""
    lay, e, dump = _ahead_of_dump(apps, "kill-swap-stop")
    e.kill9()
    assert run_stub(lay, "--restore", str(dump), "--die-at", "drop-store") == 9
    pg.stop(lay, quit_dump=False)
    b = Engine(lay)
    assert b.action == "start"
    assert "finished an interrupted restore" in b.notes
    assert rev(lay) == "1"
    b.term()


def test_dump_names_are_utc_and_sort_in_the_order_written(apps: Apps) -> None:
    lay = apps.layout("names")
    e = Engine(lay)
    a = pg.dump(lay, "compile")
    b = pg.dump(lay, "quit")
    assert a.name.startswith("ontology-") and a.name.endswith("Z-compile.dump")
    assert sorted([b, a]) == [a, b]
    assert pg.newest_dump(lay) == b
    e.term()


def test_pruning_keeps_the_newest_10_and_never_the_dump_just_written(apps: Apps) -> None:
    lay = apps.layout("prune")
    e = Engine(lay)
    lay.backups.mkdir(parents=True, exist_ok=True)
    old = [lay.backups / f"ontology-20200101T0000{i:02d}.000000Z-compile.dump" for i in range(12)]
    for f in old:
        f.write_bytes(b"")
    pg.dump(lay, "compile")
    kept = sorted(p.name for p in lay.backups.glob("ontology-*.dump"))
    assert len(kept) == 10
    assert kept[:9] == [f.name for f in old[3:]]

    # The clock went back: ten dumps sort after the one being written. It still survives.
    future = [
        lay.backups / f"ontology-20991231T0000{i:02d}.000000Z-compile.dump" for i in range(10)
    ]
    for f in future:
        f.write_bytes(b"")
    just_written = pg.dump(lay, "compile")
    assert just_written.exists()
    assert all(f.exists() for f in future)
    e.term()


def test_a_failed_dump_leaves_no_part_file(apps: Apps) -> None:
    lay = apps.layout("dump-fail")
    e = Engine(lay)
    e.term()  # the server is stopped, so pg_dump can't connect
    before = sorted(lay.backups.iterdir())
    with pytest.raises(pg.PgError, match="pg_dump"):
        pg.dump(lay, "compile")
    assert sorted(lay.backups.iterdir()) == before
