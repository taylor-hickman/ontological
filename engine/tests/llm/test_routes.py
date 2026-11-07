"""The route table (ARCHITECTURE.md §6.1, §6.3): a task names a model, as a CLI alias or a full
model ID, and an effort level."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontological.engine import main
from ontological.llm.routes import (
    DEFAULT_ROUTES,
    Route,
    fallback_for,
    load_routes,
    merge_routes,
)


def test_default_routes_follow_the_table_in_6_3() -> None:
    assert DEFAULT_ROUTES["table_keys"] == Route("sonnet", "high")
    assert DEFAULT_ROUTES["column_descriptions"] == Route("haiku", "medium")
    assert DEFAULT_ROUTES["bridge_sql"] == Route("opus", "high")
    assert DEFAULT_ROUTES["answer_agent"] == Route("opus", "medium")
    assert DEFAULT_ROUTES["ping"] == Route("haiku", "low")
    assert len(DEFAULT_ROUTES) == 13


@pytest.mark.parametrize(
    "model", ["haiku", "sonnet", "opus", "fable", "claude-opus-5-5", "claude-haiku-5-5"]
)
def test_a_route_takes_an_alias_or_a_full_model_id(model: str) -> None:
    assert Route(model, "low").model == model


@pytest.mark.parametrize("model", ["", "gpt-5", "Opus", "claude-", "claude opus", "opus[1m]x"])
def test_a_route_refuses_what_is_neither(model: str) -> None:
    with pytest.raises(ValueError, match="model"):
        Route(model, "low")


def test_a_route_refuses_an_unknown_effort() -> None:
    with pytest.raises(ValueError, match="effort"):
        Route("haiku", "extreme")


def test_opus_routes_fall_back_to_sonnet_and_nothing_else_falls_back() -> None:
    assert fallback_for("opus") == "sonnet"
    assert fallback_for("claude-opus-5-5") == "sonnet"
    assert fallback_for("sonnet") is None
    assert fallback_for("claude-sonnet-5-5") is None
    assert fallback_for("haiku") is None
    assert fallback_for("fable") is None


def test_settings_override_some_routes_and_keep_the_rest() -> None:
    routes = merge_routes({"answer_agent": Route("claude-fable-5-1", "high")})
    assert routes["answer_agent"] == Route("claude-fable-5-1", "high")
    assert routes["table_keys"] == DEFAULT_ROUTES["table_keys"]


def test_an_override_for_an_unknown_task_is_refused() -> None:
    with pytest.raises(ValueError, match="no_such_task"):
        merge_routes({"no_such_task": Route("haiku", "low")})


def test_routes_load_from_a_json_file(tmp_path: Path) -> None:
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"answer_agent": {"model": "claude-sonnet-5-5", "effort": "low"}}))
    assert load_routes(path) == {"answer_agent": Route("claude-sonnet-5-5", "low")}


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("not json", "can't read routes"),
        ("[]", "must be an object"),
        ('{"answer_agent": {"model": "claude-sonnet-5-5"}}', "model, effort"),
        ('{"answer_agent": {"model": "gpt", "effort": "low"}}', "neither a CLI alias"),
        ('{"no_such_task": {"model": "haiku", "effort": "low"}}', "no_such_task"),
    ],
)
def test_a_routes_file_that_isnt_routes_is_refused(
    tmp_path: Path, content: str, match: str
) -> None:
    path = tmp_path / "routes.json"
    path.write_text(content)
    with pytest.raises(ValueError, match=match):
        load_routes(path)


def test_the_engine_takes_its_routes_from_routes(tmp_path: Path) -> None:
    assert main.parse_args([])[0].routes is None
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"answer_review": {"model": "claude-haiku-5-5", "effort": "low"}}))
    config, _ = main.parse_args(["--routes", str(path)])
    assert config.routes == {"answer_review": Route("claude-haiku-5-5", "low")}
    path.write_text("{}x")
    with pytest.raises(SystemExit):
        main.parse_args(["--routes", str(path)])
