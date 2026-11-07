"""The route table (ARCHITECTURE.md §6.1, §6.3): which model, at which effort, does each task.

A route's model is a CLI alias, which follows new releases, or a full model ID, which pins one
(eval configs pin). Every route is a setting; `merge_routes` lays the user's over the defaults.
`load_routes` reads routes from a JSON file, as the engine's `--routes` does for the eval.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

ALIASES = frozenset({"haiku", "sonnet", "opus", "fable"})
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_MODEL_ID = re.compile(r"claude-[a-z0-9]+(?:[-.][a-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class Route:
    model: str  # a CLI alias ("haiku" | "sonnet" | "opus" | "fable") or a full model ID
    effort: str  # "low" | "medium" | "high" | "xhigh" | "max"

    def __post_init__(self) -> None:
        if self.model not in ALIASES and not _MODEL_ID.fullmatch(self.model):
            raise ValueError(f"model {self.model!r} is neither a CLI alias nor a full model ID")
        if self.effort not in EFFORTS:
            raise ValueError(f"effort {self.effort!r} is not one of {sorted(EFFORTS)}")


DEFAULT_ROUTES: Mapping[str, Route] = {
    "table_keys": Route("sonnet", "high"),
    "column_descriptions": Route("haiku", "medium"),
    "table_terms": Route("sonnet", "high"),
    "table_synonyms": Route("haiku", "low"),
    "table_semantics": Route("sonnet", "high"),
    "semantic_fk_pick": Route("sonnet", "medium"),
    "sql_attribute_judge": Route("sonnet", "medium"),
    "bridge_sql": Route("opus", "high"),
    "pii_classify": Route("haiku", "low"),
    "answer_agent": Route("opus", "medium"),
    "answer_review": Route("sonnet", "medium"),
    "answerable_extract": Route("haiku", "low"),
    "ping": Route("haiku", "low"),
}


def merge_routes(overrides: Mapping[str, Route] | None = None) -> dict[str, Route]:
    """The defaults with the user's routes laid over them. Tasks are fixed by the code, so an
    override for a task that doesn't exist is refused rather than ignored."""
    overrides = overrides or {}
    if unknown := sorted(set(overrides) - set(DEFAULT_ROUTES)):
        raise ValueError(f"routes for unknown tasks: {', '.join(unknown)}")
    return {**DEFAULT_ROUTES, **overrides}


def fallback_for(model: str) -> str | None:
    """`sonnet` for an Opus route, so an overloaded Opus or a plan without it still answers.
    Nothing else falls back: silently downgrading a judgment call is worse than waiting."""
    if model == "opus" or model.startswith("claude-opus-"):
        return "sonnet"
    return None


def load_routes(path: Path) -> dict[str, Route]:
    """`{"<task>": {"model": ..., "effort": ...}, ...}` from a JSON file. Raises ValueError on a
    file that isn't that shape, a bad model or effort, or a task that doesn't exist."""
    try:
        raw: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"can't read routes from {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: routes must be an object of task -> {{model, effort}}")
    routes: dict[str, Route] = {}
    for task, route in cast(dict[str, Any], raw).items():
        if not isinstance(route, dict) or set(cast(dict[str, Any], route)) != {"model", "effort"}:
            raise ValueError(f"{path}: the route for {task} must be {{model, effort}}")
        r = cast(dict[str, Any], route)
        routes[task] = Route(str(r["model"]), str(r["effort"]))
    merge_routes(routes)  # refuses tasks that don't exist
    return routes
