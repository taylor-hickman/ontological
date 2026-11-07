"""Eval configs, stored results, and the usage arithmetic the report reads (BUILD_PLAN.md,
Scoring and runs).

A config is `eval/configs/<name>.json`: `{"about", "critic", "routes": {task: {model, effort}}}`.
Every route pins a full model ID, so a model release can't land in the middle of a comparison
(ARCHITECTURE.md §6.3), and `answer_agent` and `answer_review` must be among them.

Results live under `<home>/results/`:

    runs/<run id>/run.json     everything a run did, written after every question
    runs/<run id>/report.md    the report, from run.json alone (`report`)
    runs/<run id>/routes.json  the routes its engine was started with
    control/<model>-<effort>.json   the control arm's answers for that model, by question

A call is a row of the engine's `ops.llm_call` (or the runner's own log, for the control), as a
dict: `task`, `kind`, `model` (the route's), `resolved_model`, `answered_model`, `fallback`,
`outcome`, `cached`, `usage` (`ResultMessage.model_usage`), `cost_usd`, `num_turns`,
`duration_ms`. `model_usage` also lists Claude Code's own side calls, on Haiku (spike S1), so
`tokens` splits each call's entries into the model that answered and the side calls.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ontological.llm.routes import ALIASES, Route, parse_routes

from .home import results_dir

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"
REQUIRED_ROUTES = ("answer_agent", "answer_review")
TOKEN_FIELDS = {
    "input": "inputTokens",
    "output": "outputTokens",
    "cache_read": "cacheReadInputTokens",
    "cache_write": "cacheCreationInputTokens",
}

Call = dict[str, Any]
Record = dict[str, Any]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    name: str
    critic: bool
    routes: dict[str, Route]

    @property
    def agent(self) -> Route:
        return self.routes["answer_agent"]

    def routes_json(self) -> dict[str, dict[str, str]]:
        return {t: {"model": r.model, "effort": r.effort} for t, r in self.routes.items()}


def load_config(name: str, directory: Path = CONFIGS_DIR) -> Config:
    """`name` is a file in eval/configs/ without `.json`, or a path to a config file."""
    path = Path(name) if name.endswith(".json") else directory / f"{name}.json"
    try:
        raw = cast(dict[str, Any], json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"can't read the eval config {path}: {e}") from e
    try:
        routes = parse_routes(raw.get("routes", {}), where=str(path))
    except ValueError as e:
        raise ConfigError(str(e)) from e
    if missing := [t for t in REQUIRED_ROUTES if t not in routes]:
        raise ConfigError(f"{path}: no route for {', '.join(missing)}")
    if aliases := sorted(t for t, r in routes.items() if r.model in ALIASES):
        raise ConfigError(
            f"{path}: eval configs pin full model IDs, and {', '.join(aliases)} use an alias"
        )
    return Config(name=path.stem, critic=bool(raw.get("critic", True)), routes=routes)


# --- where results live -------------------------------------------------------------------------


def runs_dir(home: Path) -> Path:
    return results_dir(home) / "runs"


def control_path(home: Path, route: Route) -> Path:
    return results_dir(home) / "control" / f"{route.model}-{route.effort}.json"


def write_json(path: Path, data: Any) -> None:
    """Write through a temporary file, so a crash mid-write keeps the last good copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def find_run(home: Path, name: str) -> Path:
    """A run's run.json, from its directory, its run.json, or its id under runs/."""
    for candidate in (Path(name), Path(name) / "run.json", runs_dir(home) / name / "run.json"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no run {name!r} in {runs_dir(home)}")


def question_key(source: str, qid: int) -> str:
    return f"{source}:{qid}"


# --- usage --------------------------------------------------------------------------------------


def spawns(calls: Iterable[Call]) -> int:
    """Calls that spawned `claude`: cache hits didn't."""
    return sum(1 for c in calls if not c.get("cached"))


def turns(calls: Iterable[Call]) -> int:
    return sum(int(c.get("num_turns") or 0) for c in calls)


def tokens(calls: Iterable[Call]) -> dict[str, dict[str, dict[str, float]]]:
    """`{"answering" | "side": {model ID: {input, output, cache_read, cache_write, cost_usd}}}`.

    An entry of a call's `model_usage` is the answering model's when it is the model that
    answered (or, with none recorded, the one the session resolved); every other entry is one of
    Claude Code's own side calls."""
    out: dict[str, dict[str, dict[str, float]]] = {"answering": {}, "side": {}}
    for c in calls:
        usage = cast(Mapping[str, Mapping[str, Any]], c.get("usage") or {})
        answered = c.get("answered_model") or c.get("resolved_model")
        for model, entry in usage.items():
            role = "answering" if answered is None or model == answered else "side"
            totals = out[role].setdefault(model, dict.fromkeys([*TOKEN_FIELDS, "cost_usd"], 0.0))
            for name, key in TOKEN_FIELDS.items():
                totals[name] += float(entry.get(key) or 0)
            totals["cost_usd"] += float(entry.get("costUSD") or 0)
    return out


def utilization_change(before: Mapping[str, float], after: Mapping[str, float]) -> dict[str, float]:
    """Per window, the change in `RateLimitEvent` utilization between two readings. Claude Code
    leaves the SDK's `utilization` empty and sends it per window in `raw["unifiedWindows"]`
    (spike S1); the gateway's `RateLimit.utilization` holds those."""
    return {w: after[w] - before[w] for w in sorted(set(before) & set(after))}
