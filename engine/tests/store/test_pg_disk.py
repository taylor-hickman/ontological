"""The disk-full pause (ARCHITECTURE.md §5.3, Disk). If Postgres stops on a full disk, the engine
reports it and waits; it never deletes backups to free space, and once space is freed it starts
the server again with nothing committed lost.

Each test puts <appData> on its own small disk image (macOS `hdiutil`), so filling it is quick
and touches nothing else."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import Apps, Engine, sql, stop_servers

from ontological.store import pg

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="uses hdiutil disk images")


@pytest.fixture
def volume() -> Iterator[Path]:
    """An 80 MB HFS+ volume mounted under a short path."""
    root = Path(tempfile.mkdtemp(prefix="pgdisk-", dir="/tmp"))
    mount = root / "vol"
    mount.mkdir()
    image = root / "disk.dmg"
    subprocess.run(
        ["hdiutil", "create", "-size", "80m", "-fs", "HFS+", "-volname", "pgdisk", str(image)],
        check=True, capture_output=True,
    )  # fmt: skip
    subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), str(image)],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        yield mount
    finally:
        # Stop the test's server before the volume goes: a fresh image gives the next test's
        # data directory the same inode, which Postgres uses as its shared memory key.
        stop_servers(mount / "App Support" / "Ontological")
        subprocess.run(["hdiutil", "detach", "-force", str(mount)], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


def fill(volume: Path, leave: int = 0) -> Path:
    """Fill the volume until about `leave` bytes are free, or none."""
    filler = volume / "filler"
    st = os.statvfs(volume)
    target = max(st.f_bavail * st.f_frsize - leave, 0)
    fd = os.open(filler, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        while target > 0 or leave == 0:
            target -= os.write(fd, b"\0" * min(1 << 20, max(target, 4096)))
    except OSError as e:
        assert e.errno == errno.ENOSPC
    finally:
        os.close(fd)
    return filler


def store_on(apps: Apps, volume: Path) -> pg.Layout:
    lay = pg.Layout(volume / "App Support" / "Ontological", apps.bin17)
    apps.layouts.append(lay)
    return lay


def backups(lay: pg.Layout) -> dict[str, int]:
    return {p.name: p.stat().st_size for p in lay.backups.iterdir()}


def test_postgres_stopped_by_a_full_disk_waits_for_space_then_starts(
    apps: Apps, volume: Path
) -> None:
    lay = store_on(apps, volume)
    e = Engine(lay)
    sql(
        lay,
        "CREATE TABLE t (id int PRIMARY KEY, n int);"
        "INSERT INTO t SELECT i, 0 FROM generate_series(1, 100) i; CHECKPOINT",
    )
    pg.dump(lay, "compile")
    pg.dump(lay, "compile")
    kept = backups(lay)
    e.kill9()
    postmaster = pg.read_pid_file(lay.pgdata)
    assert postmaster is not None

    filler = fill(volume, leave=256 * 1024)
    # A new WAL segment doesn't fit: the backend PANICs, recovery can't write either, and the
    # postmaster shuts down.
    committed = 0
    with pytest.raises(pg.PgError):
        for _ in range(10):
            sql(lay, "SELECT pg_switch_wal(); UPDATE t SET n = n + 1")
            committed += 1
    pg.wait_gone(postmaster.pid, 30)

    full = pg.disk_full(lay)
    assert isinstance(full, pg.DiskFull)
    assert full.free_bytes < 16 << 20
    assert "free" in str(full)
    with pytest.raises(pg.DiskFull):
        pg.ensure_running(lay)

    reports: list[pg.DiskFull] = []
    reported = threading.Event()
    result: list[pg.Server] = []

    def on_disk_full(err: pg.DiskFull) -> None:
        reports.append(err)
        reported.set()

    t = threading.Thread(
        target=lambda: result.append(
            pg.wait_until_running(lay, on_disk_full=on_disk_full, poll_s=0.2)
        )
    )
    t.start()
    assert reported.wait(30), "the pause is reported"
    t.join(1)
    assert t.is_alive(), "it waits while the disk is full"
    assert backups(lay) == kept, "never deletes backups to free space"

    filler.unlink()
    t.join(60)
    assert not t.is_alive()
    assert len(reports) == 1, "reported once, not on every poll"
    assert result and result[0].action == "start"
    assert sql(lay, "SELECT count(*), sum(n) FROM t") == f"100|{100 * committed}"
    assert pg.disk_full(lay) is None
    pg.stop(lay, quit_dump=False)


def test_a_dump_on_a_full_disk_fails_cleanly(apps: Apps, volume: Path) -> None:
    lay = store_on(apps, volume)
    e = Engine(lay)
    sql(
        lay,
        "CREATE TABLE t (id int, pad text);"
        "INSERT INTO t SELECT i, repeat(md5(i::text), 64) FROM generate_series(1, 2000) i;"
        "CHECKPOINT",
    )
    pg.dump(lay, "compile")
    kept = backups(lay)
    fill(volume)

    with pytest.raises(pg.DiskFull):
        pg.dump(lay, "compile")
    assert backups(lay) == kept, "no .part left, nothing pruned"

    # Engine shutdown on a full disk: the quit dump fails, the server still stops.
    out = e.term_error()
    assert out["type"] == "DiskFull"
    assert not (lay.pgdata / "postmaster.pid").exists()
    assert backups(lay) == kept
