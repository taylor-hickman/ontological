"""The compile-quality review (BUILD_PLAN.md M3.10): its configs, its usage summary, and Pagila
loaded into the eval cluster and ingested into a review store of its own, which `dump` reads.
The compiles themselves spend quota, so they run by hand, not here."""

from __future__ import annotations

import json
from pathlib import Path

from ontological.llm.routes import DEFAULT_ROUTES, Route
from ontological.store import pg

from ontological_eval import cluster, ingest, review
from ontological_eval.home import app_data_dir


def test_one_step_cheaper_moves_the_model_down_then_the_effort() -> None:
    assert review.one_step_cheaper(Route("opus", "high")) == Route("sonnet", "high")
    assert review.one_step_cheaper(Route("sonnet", "medium")) == Route("haiku", "medium")
    assert review.one_step_cheaper(Route("haiku", "medium")) == Route("haiku", "low")
    assert review.one_step_cheaper(Route("haiku", "low")) == Route("haiku", "low")


def test_the_cheaper_configs_move_every_compile_route_with_a_cheaper_step() -> None:
    assert {
        "table_keys": Route("haiku", "high"),
        "column_descriptions": Route("haiku", "low"),
        "table_terms": Route("haiku", "high"),
        "table_semantics": Route("haiku", "high"),
        "semantic_fk_pick": Route("haiku", "medium"),
        "sql_attribute_judge": Route("haiku", "medium"),
        "bridge_sql": Route("sonnet", "high"),
    } == review.CHEAPER
    assert set(review.COMPILE_TASKS) <= set(DEFAULT_ROUTES)
    assert {c.table_calls for c in review.CONFIGS.values()} == {"split", "merged"}


def test_usage_summary_counts_calls_tokens_and_cost_per_task_and_model() -> None:
    rows = [
        {
            "task": "table_keys",
            "model": "sonnet",
            "effort": "high",
            "outcome": "ok",
            "cost_usd": 0.01,
            "duration_ms": 2000,
            "usage": {
                "claude-sonnet-5-5": {
                    "inputTokens": 2,
                    "cacheReadInputTokens": 1000,
                    "cacheCreationInputTokens": 500,
                    "outputTokens": 300,
                    "thinkingTokens": 100,
                    "costUSD": 0.009,
                },
                "claude-haiku-5-5": {"inputTokens": 1500, "outputTokens": 20, "costUSD": 0.001},
            },
        },
        {
            "task": "table_keys",
            "model": "sonnet",
            "effort": "high",
            "outcome": "invalid",
            "cost_usd": None,
            "duration_ms": None,
            "usage": None,
        },
    ]
    s = review.usage_summary(rows)
    assert s["calls"] == 2
    assert s["cost_usd"] == 0.01
    assert s["call_seconds"] == 2.0
    assert s["by_task"]["table_keys"] == {
        "route": "sonnet/high",
        "calls": 2,
        "cost_usd": 0.01,
        "ms": 2000,
        "outcomes": {"invalid": 1},
    }
    assert s["by_model"]["claude-sonnet-5-5"] == {
        "input": 1502,
        "output": 300,
        "thinking": 100,
        "cost_usd": 0.009,
    }
    assert s["by_model"]["claude-haiku-5-5"]["input"] == 1500


def test_pagila_loads_once_and_ingests_into_a_review_store(home: Path, pg_bin: Path) -> None:
    lay = ingest.layout(home, pg_bin, "split")
    assert lay.app_data == app_data_dir(home, "split") == home / "review" / "split"
    try:
        [result] = ingest.run(home, pg_bin, [ingest.PAGILA], store="split")
        assert result.ingested.database_name == cluster.PAGILA
        cluster.up(home, pg_bin)
        cluster.ensure_pagila(home, pg_bin)  # already there: nothing reloads
        films = pg.psql(cluster.layout(home, pg_bin), "SELECT count(*) FROM film", db="pagila")
        assert films == "1000"

        server = pg.ensure_running(lay)
        tables = pg.psql(
            lay,
            "SELECT string_agg(s.name || '.' || t.name, ',' ORDER BY 1) FROM catalog_table t "
            "JOIN catalog_schema s ON s.id = t.schema_id",
        ).split(",")
        assert {"public.film", "public.rental", "analytics.film_performance"} <= set(tables)
        assert not ingest.layout(home, pg_bin).pgdata.exists()  # the eval store is untouched

        [out] = review.dump(home, pg_bin, ["split", "merged"], [ingest.PAGILA])
        dumped = json.loads(out.read_text())
        film = dumped["public.film"]
        assert set(film) == {"split"}  # no merged store: nothing to read
        assert film["split"]["terms"] == [] and film["split"]["fks"] == []
        assert "title" in film["split"]["unlinked"]
        assert server.action in {"start", "initdb+start", "adopt"}
    finally:
        pg.stop(lay, quit_dump=False)
