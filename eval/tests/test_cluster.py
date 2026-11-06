"""The eval cluster on the bundled Postgres, with a small WWI-shaped dataset.

Needs ONTOLOGICAL_PG_BIN, the bundled tree's bin/ (engine/README.md says how to get one), and
skips without it unless ONTOLOGICAL_PG_REQUIRED=1, as the store's lifecycle tests do.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import LINES
from ontological.store import pg

from ontological_eval import cluster
from ontological_eval.home import wwi_dir


def sql(home: Path, pg_bin: Path, q: str) -> str:
    return pg.psql(cluster.layout(home, pg_bin), q, db=cluster.DATABASE)


def test_up_loads_wwi_and_its_counts_match(home: Path, pg_bin: Path) -> None:
    server = cluster.up(home, pg_bin)

    assert server.action == "initdb+start"
    lay = cluster.layout(home, pg_bin)
    assert cluster.mismatches(lay, wwi_dir(home)) == {}
    assert cluster.row_counts(lay, ["sales.orders", "sales.orderlines"]) == {
        "sales.orders": 3,
        "sales.orderlines": 3,
    }
    assert sql(home, pg_bin, "SELECT Comments FROM Sales.Orders WHERE OrderID = 1") == (
        'two\nlines, "quoted"'
    )
    assert sql(home, pg_bin, "SELECT Comments IS NULL FROM Sales.Orders WHERE OrderID = 2") == "t"
    assert sql(home, pg_bin, "SELECT n FROM Sales.OrderSizes WHERE OrderID = 1") == "2"
    assert sql(home, pg_bin, "SELECT count(*) FROM pg_constraint WHERE conname = 'fk_order'") == "1"


def test_a_second_up_adopts_and_down_stops(home: Path, pg_bin: Path) -> None:
    first = cluster.up(home, pg_bin)

    again = cluster.up(home, pg_bin)

    assert (again.action, again.postmaster_pid) == ("adopt", first.postmaster_pid)
    pid_file = cluster.running(home, pg_bin)
    assert pid_file is not None and pid_file.pid == first.postmaster_pid
    cluster.down(home, pg_bin)
    assert cluster.running(home, pg_bin) is None
    assert cluster.up(home, pg_bin).action == "start"


def test_a_failed_load_leaves_no_cluster(home: Path, pg_bin: Path) -> None:
    (wwi_dir(home) / "data" / "Sales.OrderLines.csv").write_text(LINES + "13,99\n")  # no order 99

    with pytest.raises(pg.PgError, match="fk_order"):
        cluster.up(home, pg_bin)

    lay = cluster.layout(home, pg_bin)
    assert not lay.pgdata.exists()
    assert not lay.staging("initdb").exists()
    (wwi_dir(home) / "data" / "Sales.OrderLines.csv").write_text(LINES)
    assert cluster.up(home, pg_bin).action == "initdb+start"


def test_check_reports_a_table_whose_count_is_off(home: Path, pg_bin: Path) -> None:
    cluster.up(home, pg_bin)
    sql(home, pg_bin, "DELETE FROM Sales.OrderLines WHERE OrderLineID = 12")

    assert cluster.mismatches(cluster.layout(home, pg_bin), wwi_dir(home)) == {
        "sales.orderlines": (3, 2)
    }


def test_up_refuses_without_the_data(home: Path, pg_bin: Path) -> None:
    shutil.rmtree(wwi_dir(home) / "data")

    with pytest.raises(pg.PgError, match="run `fetch wwi` first"):
        cluster.up(home, pg_bin)

    assert not cluster.layout(home, pg_bin).app_data.exists()


def test_up_refuses_a_socket_path_over_103_bytes(pg_bin: Path, tmp_path: Path) -> None:
    home = tmp_path / ("h" * 80)

    with pytest.raises(pg.PgError, match="ONTOLOGICAL_EVAL_HOME"):
        cluster.up(home, pg_bin)

    assert not home.exists()
