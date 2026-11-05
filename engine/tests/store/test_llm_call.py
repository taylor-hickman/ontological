"""ops.llm_call, the gateway's cache and usage log (ARCHITECTURE.md §5.2, §6.4), on the bundled
Postgres. Needs ONTOLOGICAL_PG_BIN, as every test here does (see conftest.py)."""

from __future__ import annotations

import json
from dataclasses import replace

from conftest import Apps, Engine, sql

from ontological.llm.calls import LlmCall, PgCallLog
from ontological.store import pg

CALL = LlmCall(
    task="table_keys",
    kind="structured",
    prompt_version="1",
    cache_key="a" * 64,
    model="opus",
    effort="high",
    attempt=1,
    outcome="ok",
    output={"pk": ["id"]},
    resolved_model="claude-opus-5-5",
    answered_model="claude-opus-5-5",
    usage={"claude-opus-5-5": {"inputTokens": 120, "outputTokens": 30, "costUSD": 0.004}},
    cost_usd=0.004,
    duration_ms=2500,
    num_turns=1,
    session_id="s-1",
)


def test_a_logged_call_answers_a_lookup_by_its_key(apps: Apps) -> None:
    lay = apps.layout("llm")
    e = Engine(lay, "--chains")
    log = PgCallLog(pg.url(lay))
    try:
        assert log.lookup(CALL.cache_key) is None
        log.record(CALL)
        assert log.lookup(CALL.cache_key) == {"pk": ["id"]}
        assert log.lookup("b" * 64) is None

        row = sql(
            lay,
            "SELECT json_build_object('task', task, 'model', model, 'effort', effort, "
            "'answered', answered_model, 'usage', usage, 'cost', cost_usd, 'at', created_at "
            "IS NOT NULL) FROM ops.llm_call",
        )
        assert json.loads(row) == {
            "task": "table_keys",
            "model": "opus",
            "effort": "high",
            "answered": "claude-opus-5-5",
            "usage": CALL.usage,
            "cost": 0.004,
            "at": True,
        }
    finally:
        log.close()
        e.term()


def test_only_a_valid_structured_answer_from_the_routes_model_is_served(apps: Apps) -> None:
    lay = apps.layout("llm-rules")
    e = Engine(lay, "--chains")
    log = PgCallLog(pg.url(lay))
    try:
        log.record(replace(CALL, cache_key="1" * 64, outcome="invalid", error="pk: not a list"))
        log.record(replace(CALL, cache_key="2" * 64, kind="text", output="some text"))
        log.record(replace(CALL, cache_key="3" * 64, fallback=True))
        log.record(replace(CALL, cache_key="4" * 64, outcome="error", output=None))
        for key in "1234":
            assert log.lookup(key * 64) is None, key

        log.record(replace(CALL, output={"pk": ["old"]}))
        log.record(replace(CALL, output={"pk": ["new"]}))
        assert log.lookup(CALL.cache_key) == {"pk": ["new"]}, "the latest answer wins"
    finally:
        log.close()
        e.term()
