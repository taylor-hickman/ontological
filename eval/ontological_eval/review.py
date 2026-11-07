"""The compile-quality review (BUILD_PLAN.md M3.10): Pagila and WWI compiled four ways, each in a
store of its own, for a hand review of what each wrote.

- `split` and `merged` are `compile.table_calls` on the default routes (ARCHITECTURE.md §6.3).
- `split-cheaper` and `merged-cheaper` move every compile route one step cheaper: a Sonnet route
  to Haiku and Opus to Sonnet, at the same effort, and a Haiku route one effort level down. Only
  `table_synonyms` (`haiku`/`low`) has no cheaper step. The post-stages move with them, so one
  run judges every compile route, and each stage's output is read on its own.

`run` ingests each database into the config's store (`<home>/review/<config>`) and compiles it
there, so every config starts from no Terms and no cache. It records each compile's wall time,
the gateway's utilization before and after it, and from the store's call log its calls, tokens
and API-equivalent cost per task and model, in `<home>/results/compile-review.json`. A config
whose compile is already finished makes no call and keeps the figures its first run recorded.

`dump` reads what each config wrote, table by table, into
`<home>/results/compile-review-<database>.json`, the file the review reads side by side.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ontological.compile.packing import TableCalls
from ontological.llm.routes import DEFAULT_ROUTES, Route
from ontological.store import pg

from . import compile_store, ingest
from .home import app_data_dir, results_dir

DATABASES = (ingest.PAGILA, ingest.WWI)
COMPILE_TASKS = (
    "table_keys",
    "column_descriptions",
    "table_terms",
    "table_synonyms",
    "table_semantics",
    "semantic_fk_pick",
    "sql_attribute_judge",
    "bridge_sql",
    "pii_classify",
)
_EFFORT_DOWN = {"max": "xhigh", "xhigh": "high", "high": "medium", "medium": "low"}
_MODEL_DOWN = {"opus": "sonnet", "sonnet": "haiku"}


class ReviewError(RuntimeError):
    pass


def one_step_cheaper(route: Route) -> Route:
    """Opus to Sonnet or Sonnet to Haiku at the same effort; a Haiku route one effort level down;
    the route itself when there is no cheaper step."""
    if route.model in _MODEL_DOWN:
        return Route(_MODEL_DOWN[route.model], route.effort)
    return Route(route.model, _EFFORT_DOWN.get(route.effort, route.effort))


CHEAPER: Mapping[str, Route] = {
    task: cheaper
    for task in COMPILE_TASKS
    if (cheaper := one_step_cheaper(DEFAULT_ROUTES[task])) != DEFAULT_ROUTES[task]
}


@dataclass(frozen=True)
class Config:
    name: str
    table_calls: TableCalls
    routes: Mapping[str, Route]


CONFIGS: Mapping[str, Config] = {
    c.name: c
    for c in (
        Config("split", "split", {}),
        Config("merged", "merged", {}),
        Config("split-cheaper", "split", CHEAPER),
        Config("merged-cheaper", "merged", CHEAPER),
    )
}


def run(
    home: Path,
    pg_bin: Path,
    configs: Sequence[str],
    names: Sequence[str],
    *,
    progress: Callable[[str], None] = lambda _: None,
) -> dict[str, Any]:
    """Ingest and compile each database under each config; return and write the measurements.
    Stops at the first compile Claude stopped, leaving it for the next run to resume."""
    out = results_dir(home) / "compile-review.json"
    report: dict[str, Any] = json.loads(out.read_text()) if out.is_file() else {}
    for name in configs:
        config = CONFIGS[name]
        progress(f"{name}: ingesting {', '.join(names)}")
        ingest.run(home, pg_bin, list(names), store=name)
        for db in names:
            progress(f"{name}: compiling {db}")
            [result] = compile_store.run(
                home,
                pg_bin,
                [db],
                store=name,
                table_calls=config.table_calls,
                routes=config.routes,
                progress=progress,
            )
            measured = _measure(home, pg_bin, config, result)
            if measured["calls"] or f"{name}/{db}" not in report:
                report[f"{name}/{db}"] = measured
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2) + "\n")
            if not result.compiled.complete:
                raise ReviewError(
                    f"{name}: Claude stopped the {db} compile ({result.compiled.stopped}); "
                    "run the review again to resume it"
                )
    return report


def _measure(
    home: Path, pg_bin: Path, config: Config, result: compile_store.Result
) -> dict[str, Any]:
    c = result.compiled
    with _store(home, pg_bin, config.name) as lay:
        rows = _json(
            lay,
            f"""
            SELECT task, model, effort, outcome, usage, cost_usd, duration_ms
            FROM ops.llm_call WHERE job_id = {int(c.job_id)} AND NOT cached
            """,
        )
    return {
        "config": config.name,
        "database": result.name,
        "table_calls": config.table_calls,
        "routes": {
            t: f"{r.model}/{r.effort}"
            for t, r in {**DEFAULT_ROUTES, **config.routes}.items()
            if t in COMPILE_TASKS
        },
        "tables": len(c.tables),
        "complete": c.complete,
        "seconds": round(result.seconds, 1),
        "utilization_before": result.utilization_before,
        "utilization_after": result.utilization_after,
        **usage_summary(rows),
    }


def usage_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Calls, outcomes, API-equivalent cost, summed call time and tokens, per task and per
    answering model, from `ops.llm_call` rows. Tokens are each model's `model_usage`: input
    counts cache reads and writes too, since all of them come out of the quota."""
    by_task: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, float]] = {}
    calls = 0
    cost = 0.0
    for row in rows:
        calls += 1
        cost += row.get("cost_usd") or 0.0
        task = by_task.setdefault(
            row["task"],
            {"route": f"{row['model']}/{row['effort']}", "calls": 0, "cost_usd": 0.0, "ms": 0},
        )
        task["calls"] += 1
        task["cost_usd"] += row.get("cost_usd") or 0.0
        task["ms"] += row.get("duration_ms") or 0
        if row["outcome"] != "ok":
            task.setdefault("outcomes", {}).setdefault(row["outcome"], 0)
            task["outcomes"][row["outcome"]] += 1
        for model, u in cast(dict[str, dict[str, Any]], row.get("usage") or {}).items():
            m = by_model.setdefault(
                model, {"input": 0, "output": 0, "thinking": 0, "cost_usd": 0.0}
            )
            m["input"] += (
                (u.get("inputTokens") or 0)
                + (u.get("cacheReadInputTokens") or 0)
                + (u.get("cacheCreationInputTokens") or 0)
            )
            m["output"] += u.get("outputTokens") or 0
            m["thinking"] += u.get("thinkingTokens") or 0
            m["cost_usd"] += u.get("costUSD") or 0.0
    for t in by_task.values():
        t["cost_usd"] = round(t["cost_usd"], 4)
    for m in by_model.values():
        m["cost_usd"] = round(m["cost_usd"], 4)
    return {
        "calls": calls,
        "cost_usd": round(cost, 4),
        "call_seconds": round(sum(t["ms"] for t in by_task.values()) / 1000, 1),
        "by_task": by_task,
        "by_model": by_model,
    }


# --- what each config wrote --------------------------------------------------------------------

_TABLES = """
SELECT coalesce(json_agg(x ORDER BY x.table), '[]') FROM (
  SELECT s.name || '.' || t.name AS table, t.pk, t.is_junction_table AS junction,
    t.description,
    (SELECT coalesce(json_agg(json_build_object(
        'name', te.name, 'description', te.description, 'synonyms', te.synonyms,
        'attributes', (SELECT coalesce(json_agg(json_build_object(
            'name', a.name, 'column', a.source_column, 'description', a.description)
            ORDER BY a.source_column, a.name), '[]')
          FROM column_attribute a JOIN column_attribute__term at ON at.attribute_id = a.id
          WHERE at.term_id = te.id AND a.table_id = t.id)
      ) ORDER BY te.name), '[]')
     FROM table__term tt JOIN term te ON te.id = tt.term_id WHERE tt.table_id = t.id) AS terms,
    (SELECT coalesce(json_agg(json_build_object(
        'column', c.name,
        'declared', EXISTS (SELECT 1 FROM column__foreign_key f WHERE f.source_column_id = c.id),
        'target', ts.name || '.' || tt2.name || '.' || coalesce(a.source_column, a.name)
      ) ORDER BY c.name, tt2.name), '[]')
     FROM catalog_column c
     JOIN column__semantic_fk sf ON sf.column_id = c.id
     JOIN column_attribute a ON a.id = sf.attribute_id
     JOIN catalog_table tt2 ON tt2.id = a.table_id
     JOIN catalog_schema ts ON ts.id = tt2.schema_id
     WHERE c.table_id = t.id) AS fks,
    (SELECT coalesce(json_agg(c.name ORDER BY c.ordinal_position), '[]')
     FROM catalog_column c
     WHERE c.table_id = t.id
       AND NOT EXISTS (SELECT 1 FROM column__has_attribute h WHERE h.column_id = c.id)
       AND NOT EXISTS (SELECT 1 FROM column__semantic_fk sf WHERE sf.column_id = c.id)
    ) AS unlinked
  FROM catalog_table t
  JOIN catalog_schema s ON s.id = t.schema_id
  JOIN catalog_database d ON d.id = s.database_id
  WHERE d.name = {database}
) x
"""


def dump(home: Path, pg_bin: Path, configs: Sequence[str], names: Sequence[str]) -> list[Path]:
    """Write each database's tables as each config compiled them, keyed table then config."""
    written: list[Path] = []
    per_db: dict[str, dict[str, dict[str, Any]]] = {db: {} for db in names}
    for name in configs:
        if not app_data_dir(home, name).is_dir():
            continue
        with _store(home, pg_bin, name) as lay:
            for db in names:
                database = ingest.source(home, pg_bin, db).database_name
                for table in _json(lay, _TABLES.format(database=_literal(database))):
                    per_db[db].setdefault(table.pop("table"), {})[name] = table
    for db, tables in per_db.items():
        out = results_dir(home) / f"compile-review-{db}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(dict(sorted(tables.items())), indent=2) + "\n")
        written.append(out)
    return written


class _store:
    """The review store `name`'s Postgres, started for the block if it wasn't running."""

    def __init__(self, home: Path, pg_bin: Path, name: str) -> None:
        self.lay = ingest.layout(home, pg_bin, name)

    def __enter__(self) -> pg.Layout:
        self.server = pg.ensure_running(self.lay)
        return self.lay

    def __exit__(self, *_: object) -> None:
        if self.server.action != "adopt":
            pg.stop(self.lay)


def _json(lay: pg.Layout, query: str) -> list[dict[str, Any]]:
    """Rows of `query` as dicts: the query's own JSON array, or each row as JSON."""
    q = query.strip()
    sql = (
        q
        if q.startswith("SELECT coalesce(json_agg")
        else (f"SELECT coalesce(json_agg(r), '[]') FROM ({q}) r")
    )
    return cast(list[dict[str, Any]], json.loads(pg.psql(lay, sql) or "[]"))


def _literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"
