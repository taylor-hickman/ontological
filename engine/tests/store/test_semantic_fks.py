"""Semantic FK inference in a compile (BUILD_PLAN.md M3.4): a shop with no declared foreign keys,
compiled with the scripted gateway of test_compile.py and embedded by the real model into the
bundled store. Claude is asked for the columns in one batch; an edge whose column shares no value
with its target is pruned, and a column Claude passed on is matched by its sample values, both
through the source executor's `probe` op. Needs the model and the binaries."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import sql
from fakes import NoAnswer, ScriptedGateway
from test_compile import claude as chinook_claude
from test_compile import sections, table_of

from ontological.catalog.ingest import ingest
from ontological.compile.semantic_fk import BATCH_PROMPT_VERSION, SemanticFks
from ontological.compile.split import Compiled, compile_database
from ontological.embed.embedder import LocalEmbedder
from ontological.sources.executor import SourceExecutor, SqliteSource
from ontological.store import pg

# Names and titles repeat, so the only unique attributes off `orders` are the two keys.
SHOP = """
CREATE TABLE customer (customer_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE product (sku INTEGER PRIMARY KEY, title TEXT);
CREATE TABLE orders (
  order_id INTEGER PRIMARY KEY, customer_id INTEGER, product_ref INTEGER, sku INTEGER
);
INSERT INTO customer VALUES (1, 'Ann'), (2, 'Ann'), (3, 'Bo');
INSERT INTO product VALUES (10, 'Pen'), (11, 'Pen'), (12, 'Ink');
INSERT INTO orders VALUES (100, 1, 900, 10), (101, 3, 901, 12), (102, 3, 902, 12);
"""
FK_COLUMNS = ["customer_id", "product_ref", "sku"]
# What Claude picks for each column, by the description of the attribute it points to, which
# the scripted descriptions stage wrote: product_ref's pick shares no value with it.
PICKS = {"customer_id": "About customer_id.", "product_ref": "About sku.", "sku": None}


def pick(section: str) -> str | None:
    found = re.search(r"^Foreign-key column: (\w+) ", section, flags=re.M)
    assert found, section
    wanted = PICKS[found.group(1)]
    if wanted is None:
        return None
    for line in section.splitlines():
        if line.startswith("column_id=") and wanted in line:
            return line.removeprefix("column_id=").split(" |", 1)[0]
    raise AssertionError(f"no candidate {wanted!r} in {section}")


def claude(task: str, user: str) -> dict[str, Any] | NoAnswer | None:
    if task == "semantic_fk_pick":
        parts = [p for p in re.split(r"^(?=Foreign-key column: )", user, flags=re.M) if p.strip()]
        if len(parts) == 1:
            return {"selected_id": pick(parts[0])}
        entries: list[dict[str, Any]] = []
        for part in parts:
            heading = part.split("\n", 1)[0].removeprefix("Foreign-key column: ")
            entries.append({"column": heading, "selected_id": pick(part)})
        return {"columns": entries}
    if task == "table_keys":

        def keys(table: str) -> dict[str, Any]:
            names = FK_COLUMNS if table == "orders" else []
            return {"fk_suggestions": [{"column_name": n} for n in names]}

        found = sections(user)
        if len(found) > 1:
            return {"tables": [{"table": h, **keys(h.removeprefix("main."))} for h, _ in found]}
        return keys(table_of(user))
    return chinook_claude(task, user)


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    path = tmp_path / "shop.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SHOP)
    con.commit()
    con.close()
    return path


def compile_shop(
    path: Path, gateway: ScriptedGateway, embedder: LocalEmbedder, *, ingested: bool = False
) -> Compiled:
    with SourceExecutor(SqliteSource(path)) as executor:
        name = "shop" if ingested else ingest(executor).database_name
        return asyncio.run(compile_database(name, executor, gateway, embedder=embedder))


def edges(store: pg.Layout) -> list[str]:
    out = sql(
        store,
        "SELECT st.name || '.' || sc.name || ' -> ' || tt.name || '.' || tc.name "
        "FROM column__semantic_fk f "
        "JOIN catalog_column sc ON sc.id = f.column_id "
        "JOIN catalog_table st ON st.id = sc.table_id "
        "JOIN column__has_attribute h ON h.attribute_id = f.attribute_id "
        "JOIN catalog_column tc ON tc.id = h.column_id "
        "JOIN catalog_table tt ON tt.id = tc.table_id ORDER BY 1",
    )
    return out.splitlines() if out else []


def fk_calls(gateway: ScriptedGateway) -> list[str]:
    return [c.user for c in gateway.calls if c.task == "semantic_fk_pick"]


def test_fks_are_picked_in_one_batch_then_pruned_and_matched_through_probes(
    store: pg.Layout, shop: Path, embedder: LocalEmbedder
) -> None:
    gateway = ScriptedGateway(claude)

    got = compile_shop(shop, gateway, embedder)

    assert got.complete
    [batch] = [c for c in gateway.calls if c.task == "semantic_fk_pick"]
    assert batch.prompt_version == BATCH_PROMPT_VERSION
    assert re.findall(r"^Foreign-key column: (.+)$", batch.user, flags=re.M) == [
        "customer_id (table: orders)",
        "product_ref (table: orders)",
        "sku (table: orders)",
    ]
    # Each column's candidates are the two unique attributes off its own table
    assert batch.user.count("column_id=") == 6
    assert edges(store) == [
        "orders.customer_id -> customer.customer_id",
        "orders.sku -> product.sku",
    ]
    assert got.semantic_fks == SemanticFks(
        declared=0, self_references=0, inferred=2, matched=1, unresolved=0, pruned=1
    )


def test_a_second_compile_infers_nothing(
    store: pg.Layout, shop: Path, embedder: LocalEmbedder
) -> None:
    compile_shop(shop, ScriptedGateway(claude), embedder)
    gateway = ScriptedGateway(claude)

    again = compile_shop(shop, gateway, embedder, ingested=True)

    assert gateway.calls == []
    assert again.semantic_fks == SemanticFks(0, 0, 0, 0, 1, 0)  # product_ref, pruned before
    assert edges(store) == [
        "orders.customer_id -> customer.customer_id",
        "orders.sku -> product.sku",
    ]


def test_a_compile_stopped_at_its_fks_asks_the_same_batch_when_resumed(
    store: pg.Layout, shop: Path, embedder: LocalEmbedder
) -> None:
    def logged_out(task: str, user: str) -> dict[str, Any] | NoAnswer | None:
        if task == "semantic_fk_pick":
            return NoAnswer("error", "needs_login")
        return claude(task, user)

    first = ScriptedGateway(logged_out)
    stopped = compile_shop(shop, first, embedder)

    assert (stopped.complete, stopped.semantic_fks) == (False, None)
    assert edges(store) == []

    resumed = ScriptedGateway(claude)
    done = compile_shop(shop, resumed, embedder, ingested=True)

    assert done.complete
    assert fk_calls(resumed) == fk_calls(first)  # so the cache answers it
    assert [c.task for c in resumed.calls] == ["semantic_fk_pick"]
    assert edges(store) == [
        "orders.customer_id -> customer.customer_id",
        "orders.sku -> product.sku",
    ]
