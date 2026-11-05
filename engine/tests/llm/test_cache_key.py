"""The structured-call cache key (ARCHITECTURE.md §6.4): sha256 of task, prompt_version, model,
effort, system, user and schema, plus the caller's extra. Effort is in it because it changes the
output; the model is the route's string as written, so an alias survives a model release."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from ontological.llm.calls import cache_key


class Keys(BaseModel):
    pk: list[str]


class Terms(BaseModel):
    terms: list[str]


BASE: dict[str, Any] = {
    "task": "table_keys",
    "prompt_version": "1",
    "model": "sonnet",
    "effort": "high",
    "system": "You find keys.",
    "user": "table: orders",
    "schema": Keys,
    "extra": "",
}


def test_the_key_is_a_stable_sha256() -> None:
    key = cache_key(**BASE)
    assert len(key) == 64 and int(key, 16) >= 0
    assert cache_key(**BASE) == key


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task", "table_terms"),
        ("prompt_version", "2"),
        ("model", "claude-sonnet-5-5"),
        ("effort", "medium"),
        ("system", "You find keys!"),
        ("user", "table: order"),
        ("schema", Terms),
        ("extra", "sample-v2"),
        ("schema", None),
    ],
)
def test_every_part_changes_the_key(field: str, value: Any) -> None:
    assert cache_key(**(BASE | {field: value})) != cache_key(**BASE)


def test_parts_cannot_run_into_each_other() -> None:
    ab_c: dict[str, Any] = {"system": "ab", "user": "c"}
    a_bc: dict[str, Any] = {"system": "a", "user": "bc"}
    assert cache_key(**(BASE | ab_c)) != cache_key(**(BASE | a_bc))
