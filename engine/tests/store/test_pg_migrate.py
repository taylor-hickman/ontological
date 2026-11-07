"""The store's two Alembic chains (ARCHITECTURE.md §5.2) and running them (§5.3, First launch
and Migrations; §12, "failed migration then restore"): upstream's vendored chain owns `public`,
Ontological's owns `app` and `ops` and keeps its version table in `ops`."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from conftest import STUB, Apps, Engine, count, nearest, seed, sql

from ontological.store import chains, pg

UPSTREAM_HEAD = "99d4d343dd0b"  # upstream's chain at 2a24f5d
OURS_HEAD = "0006"
GROWN = f"{int(OURS_HEAD) + 1:04d}"  # the first revision `extended` adds


def upstream_rev(lay: pg.Layout) -> str:
    return sql(lay, "SELECT version_num FROM public.alembic_version")


def ours_rev(lay: pg.Layout) -> str:
    return sql(lay, "SELECT version_num FROM ops.alembic_version")


def exists(lay: pg.Layout, relation: str) -> bool:
    return sql(lay, f"SELECT to_regclass('{relation}') IS NOT NULL") == "t"


def schemas(lay: pg.Layout) -> set[str]:
    return set(sql(lay, "SELECT nspname FROM pg_namespace").split())


def extended(tmp_path: Path, *revisions: str) -> tuple[chains.Chain, ...]:
    """Both chains, with Ontological's copied and grown by one revision per SQL string, numbered
    on from its head, each in its own transaction."""
    scripts = tmp_path / "migrations"
    shutil.copytree(chains.ONTOLOGICAL.scripts, scripts)
    down = OURS_HEAD
    for i, q in enumerate(revisions, start=int(OURS_HEAD) + 1):
        rev = f"{i:04d}"
        (scripts / "versions" / f"{rev}_test.py").write_text(
            "from alembic import op\n\n"
            f"revision = {rev!r}\ndown_revision = {down!r}\n"
            "branch_labels = None\ndepends_on = None\n\n\n"
            f"def upgrade() -> None:\n    op.execute({q!r})\n\n\n"
            "def downgrade() -> None:\n    raise NotImplementedError\n"
        )
        down = rev
    return (chains.UPSTREAM, chains.Chain(chains.ONTOLOGICAL.name, scripts, "ops"))


CONVERSATION = "CREATE TABLE app.conversation (id uuid PRIMARY KEY DEFAULT gen_random_uuid())"
FAILS = "CREATE TABLE app.message (id int); SELECT 1 / 0"


def test_first_launch_runs_both_chains(apps: Apps) -> None:
    lay = apps.layout("first")
    e = Engine(lay, "--chains")
    assert upstream_rev(lay) == UPSTREAM_HEAD
    assert ours_rev(lay) == OURS_HEAD
    assert {"public", "app", "ops"} <= schemas(lay)
    assert "frontend" not in schemas(lay), "upstream's Prisma schema is never created"
    assert exists(lay, "public.catalog_database")
    assert sql(lay, "SELECT count(*) FROM public.alembic_version") == "1", "one row: upstream's"
    assert chains.pending(pg.url(lay)) == []
    assert pg.migrate(lay) is None, "nothing pending: no dump"
    assert not list(lay.backups.glob("*-premigrate.dump"))
    e.term()


def test_migrate_brings_an_unmigrated_store_to_both_heads(apps: Apps) -> None:
    lay = apps.layout("unmigrated")
    e = Engine(lay)  # a store with no chains applied
    assert chains.pending(pg.url(lay)) == ["upstream", "ontological"]
    dump = pg.migrate(lay)
    assert dump is not None and dump.name.endswith("-premigrate.dump")
    assert upstream_rev(lay) == UPSTREAM_HEAD and ours_rev(lay) == OURS_HEAD
    assert pg.migrate(lay) is None
    e.term()


def test_two_engines_migrating_at_once_take_turns(apps: Apps) -> None:
    """Without pg.lock around the whole migration, both see the chains pending and dump the
    unmigrated store; one's upgrade commits, the other's fails on the same tables, and its
    restore drops the store and puts the unmigrated dump back over the first's migration."""
    lay = apps.layout("concurrent")
    e = Engine(lay)  # unmigrated
    procs = [
        subprocess.Popen(
            [sys.executable, str(STUB), str(lay.app_data), str(lay.bin), "--migrate"],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        for _ in range(3)
    ]
    outs = [p.communicate(timeout=120)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], outs
    migrated = [json.loads(o.splitlines()[-1])["migrated"] for o in outs]
    assert sum(m is not None for m in migrated) == 1, "one migrates; the others find it done"
    assert len(list(lay.backups.glob("*-premigrate.dump"))) == 1
    assert upstream_rev(lay) == UPSTREAM_HEAD and ours_rev(lay) == OURS_HEAD
    e.term()


def test_a_new_revision_runs_only_where_it_is_pending(apps: Apps, tmp_path: Path) -> None:
    lay = apps.layout("pending")
    e = Engine(lay, "--chains")
    grown = extended(tmp_path, CONVERSATION)
    assert chains.pending(pg.url(lay), grown) == ["ontological"]
    assert pg.migrate(lay, chains=grown) is not None
    assert ours_rev(lay) == GROWN and exists(lay, "app.conversation")
    assert upstream_rev(lay) == UPSTREAM_HEAD
    e.term()


def test_failed_migration_then_restore(apps: Apps, tmp_path: Path) -> None:
    lay = apps.layout("migrate")
    e = Engine(lay, "--chains")
    seed(lay, 1, 500)
    before = nearest(lay)
    # The engine's pool holds connections while migrations run.
    held = subprocess.Popen(
        [lay.tool("psql"), "-X", "-h", str(lay.pgsocket), "-p", str(pg.PORT), "-U", pg.USER,
         "-d", pg.DATABASE, "-c", "SELECT pg_sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    time.sleep(0.3)

    with pytest.raises(pg.MigrationFailed, match="division by zero") as failed:
        pg.migrate(lay, chains=extended(tmp_path, CONVERSATION, FAILS))
    dump = failed.value.restored_from
    assert dump.name.endswith("-premigrate.dump") and dump.is_file()
    held.wait(10)
    assert held.returncode != 0, "restore ends the pool's connections"
    assert ours_rev(lay) == OURS_HEAD, f"{GROWN} had committed; the restore undid it"
    assert not exists(lay, "app.conversation") and not exists(lay, "app.message")
    assert upstream_rev(lay) == UPSTREAM_HEAD
    assert count(lay) == 500
    assert nearest(lay) == before, "the HNSW index is back"
    assert pg.RESTORE_DATABASE not in sql(lay, "SELECT datname FROM pg_database", db="postgres")
    e.term()


def test_a_failure_in_the_second_chain_also_undoes_the_first(apps: Apps, tmp_path: Path) -> None:
    lay = apps.layout("both-chains")
    e = Engine(lay)
    with pytest.raises(pg.MigrationFailed, match="division by zero"):
        pg.migrate(lay, chains=extended(tmp_path, FAILS))
    assert not exists(lay, "public.alembic_version"), "upstream's chain had committed"
    assert not exists(lay, "public.catalog_database")
    assert "app" not in schemas(lay) and "ops" not in schemas(lay)
    e.term()


def test_a_migration_whose_restore_fails_too_says_so(
    apps: Apps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = apps.layout("migrate-both")
    e = Engine(lay, "--chains")
    real_run = pg.run

    def run(cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if Path(cmd[0]).name == "pg_restore":
            raise pg.PgError("pg_restore failed (1): simulated")
        return real_run(cmd, check=check)

    monkeypatch.setattr(pg, "run", run)
    with pytest.raises(pg.PgError, match="so did restoring") as failed:
        pg.migrate(lay, chains=extended(tmp_path, CONVERSATION, FAILS))
    assert not isinstance(failed.value, pg.MigrationFailed), "no restore happened"
    assert "as the failed migration left it" in str(failed.value)
    assert ours_rev(lay) == GROWN and exists(lay, "app.conversation")
    assert not exists(lay, "app.message"), "the failed revision rolled back on its own"
    monkeypatch.undo()
    e.term()
