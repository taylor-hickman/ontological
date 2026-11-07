"""The eval runner (BUILD_PLAN.md M1.9): one command runs a set and stores what it did.

    uv run python -m ontological_eval run smoke [--config sonnet] [--limit N] [--no-control]

**The agent arm** goes through the engine, as every client does (ARCHITECTURE.md §4.4). The
runner starts an engine on the eval store (`<home>/app`) with `--routes` from the config, whose
routes pin full model IDs, holds one lease across the run, and asks each question with
`POST /ask`, passing the question's database as the scope (§8.1). It refuses while an engine
already serves the eval store, since that engine's routes may not be the config's, and stops the
engine it started when it is done. A BIRD question's evidence goes with it.

**Scoring** is `verify.py`'s: the gold SQL runs natively, and the agent's SQL (the SQL its
answer carries as `validate_sql` passed it, else the answer's own) runs through Ontological's
source executor, here in the runner, uncapped up to `MAX_ROWS`.

**Usage.** Each question's calls are the engine's `ops.llm_call` rows logged while it ran: the
runner asks one question at a time and is the engine's only client. Rows for a task that isn't
answering's (a compile the engine resumed, say) are counted apart and the report warns of them.
Utilization comes from the runner's own `ping`s, before the run, after the agent arm and after
the control arm: each ping's `RateLimitEvent` carries the current utilization per window.

**The control arm** (`control.py`) runs in the runner, through its own gateway, on the agent's
route, for the questions its store for that model has no answer to.

A rate-limited session stops the arm it is in. `run.json` is written after every question, so a
run that stops or fails keeps what it did, and its report says so.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from ontological.answer.service import Asked
from ontological.cli import post_ask
from ontological.embed import model as models
from ontological.engine import discovery
from ontological.llm.calls import LlmCall
from ontological.llm.gateway import ClaudeGateway
from ontological.mcp.link import EngineUnavailable, hold_engine
from ontological.sources.executor import SourceExecutor
from ontological.store import pg

from . import cluster, control, ingest, report, runs, sets, verify
from .home import models_dir
from .questions import Question
from .verify import Rows

CLIENT = "ontological eval"
MAX_ROWS = 100_000  # rows of a predicted result the verifier compares
PRED_TIMEOUT_S = 60.0
ENGINE_STOP_S = 120.0
# The tasks answering a question calls. Anything else the engine logs meanwhile isn't the
# question's.
ANSWER_TASKS = frozenset({"answer_agent", "answer_review", "answerable_extract"})
CALL_COLUMNS = (
    "id, task, kind, model, effort, outcome, error, resolved_model, answered_model, fallback, "
    "usage, cost_usd, duration_ms, num_turns, cached"
)

# A store ingested before M2.6 has no source recorded for its databases; ingest records it.
NO_SOURCE_HINT = ". Run `ingest` again: it records each database's source, which /ask opens"

Progress = Callable[[str], None]


class RunError(RuntimeError):
    pass


class MemoryCallLog:
    """The runner's gateway log. It never serves from the cache, so the control arm asks every
    time it runs; its store is what keeps it to once per model."""

    def __init__(self) -> None:
        self.calls: list[LlmCall] = []

    def lookup(self, cache_key: str) -> Any | None:
        return None

    def record(self, call: LlmCall) -> None:
        self.calls.append(call)


def call_dict(call: LlmCall) -> runs.Call:
    keep = [
        "task",
        "kind",
        "model",
        "effort",
        "outcome",
        "error",
        "resolved_model",
        "answered_model",
        "fallback",
        "usage",
        "cost_usd",
        "duration_ms",
        "num_turns",
        "cached",
    ]
    d = asdict(call)
    return {k: d[k] for k in keep}


# --- the questions ------------------------------------------------------------------------------


def select(
    set_name: str,
    questions: Mapping[sets.Key, Question],
    *,
    limit: int | None = None,
    only: Sequence[str] = (),
) -> list[Question]:
    """The set's questions in its order: those `only` names (`wwi:12`, `bird:1312`), else all,
    then the first `limit`."""
    keys = sets.read()[set_name]
    if only:
        wanted = {_parse_key(k) for k in only}
        if extra := wanted - set(keys):
            names = ", ".join(runs.question_key(*k) for k in sorted(extra))
            raise RunError(f"not in {set_name}: {names}")
        keys = [k for k in keys if k in wanted]
    if limit is not None:
        keys = keys[:limit]
    return [questions[k] for k in keys]


def _parse_key(text: str) -> sets.Key:
    source, _, qid = text.partition(":")
    if source not in ("wwi", "bird") or not qid.isdigit():
        raise RunError(f"{text!r} isn't a question: write wwi:<id> or bird:<id>")
    return ("wwi" if source == "wwi" else "bird", int(qid))


def ask_text(q: Question) -> str:
    """What the agent is asked: the question, and BIRD's evidence, as the benchmark gives it."""
    return f"{q.question}\nEvidence: {q.evidence}" if q.evidence.strip() else q.question


# --- scoring ------------------------------------------------------------------------------------


def predicted_sql(asked: Asked) -> tuple[str | None, bool]:
    """The SQL to score, and whether `validate_sql` passed it."""
    if asked.sql:
        return asked.sql, True
    if asked.answer is not None and asked.answer.sql:
        return asked.answer.sql, False
    return None, False


class Scorer:
    """Gold SQL natively, once per question; the agent's SQL through the source executor; the
    control's natively, as its schema's dialect is the source's own."""

    def __init__(
        self,
        home: Path,
        pg_bin: Path,
        gold: sets.GoldRunner,
        *,
        open_executor: Callable[[Question], SourceExecutor] | None = None,
    ) -> None:
        self.gold_runner = gold
        self._home = home
        self._pg_bin = pg_bin
        self._open = open_executor or self._source_executor
        self._executors: dict[str, SourceExecutor] = {}
        self._gold: dict[sets.Key, Rows | Exception] = {}

    def _source_executor(self, q: Question) -> SourceExecutor:
        name = ingest.WWI if q.source == "wwi" else q.db
        return SourceExecutor(ingest.source(self._home, self._pg_bin, name))

    def gold(self, q: Question) -> Rows | Exception:
        if q.key not in self._gold:
            try:
                self._gold[q.key] = self.gold_runner(q)
            except Exception as e:
                self._gold[q.key] = e
        return self._gold[q.key]

    def agent(self, q: Question, sql: str | None, *, no_answer: bool) -> verify.Scored:
        gold = self.gold(q)
        if sql is None:
            return verify.score(gold, None, no_answer=no_answer)
        if q.db not in self._executors:
            self._executors[q.db] = self._open(q)
        try:
            got = self._executors[q.db].fetch(sql, max_rows=MAX_ROWS, timeout_s=PRED_TIMEOUT_S)
        except Exception as e:
            return verify.score(gold, e)
        return verify.score(gold, got.rows, pred_truncated=got.truncated)

    def control(self, q: Question, sql: str | None, *, no_answer: bool) -> verify.Scored:
        gold = self.gold(q)
        if sql is None:
            return verify.score(gold, None, no_answer=no_answer)
        try:
            rows, truncated = self.gold_runner.query(q, sql, max_rows=MAX_ROWS)
        except Exception as e:
            return verify.score(gold, e)
        return verify.score(gold, rows, pred_truncated=truncated)

    def close(self) -> None:
        for ex in self._executors.values():
            ex.close()
        self._executors.clear()


def _question(q: Question) -> runs.Record:
    return {
        "key": runs.question_key(*q.key),
        "source": q.source,
        "id": q.id,
        "db": q.db,
        "difficulty": q.difficulty,
    }


def agent_record(
    q: Question, asked: Asked, calls: list[runs.Call], scored: verify.Scored, seconds: float
) -> runs.Record:
    sql, validated = predicted_sql(asked)
    answer = asked.answer
    return {
        **_question(q),
        "verified": asked.verified,
        "reason": asked.reason,
        "outcome": asked.outcome,
        "error": asked.error,
        "answered_model": asked.answered_model,
        "answer": answer.answer if answer else None,
        "answerable": answer.answerable if answer else None,
        "sql": sql,
        "sql_validated": validated,
        "steps": [s.name + (" (refused)" if s.is_error else "") for s in asked.steps],
        "score": scored.to_json(),
        "calls": [c for c in calls if c["task"] in ANSWER_TASKS],
        "other_calls": [c["task"] for c in calls if c["task"] not in ANSWER_TASKS],
        "seconds": round(seconds, 1),
    }


# --- the run ------------------------------------------------------------------------------------


def run(
    home: Path,
    pg_bin: Path,
    set_name: str,
    config: runs.Config,
    questions: Sequence[Question],
    *,
    with_control: bool = True,
    in_set: int | None = None,
    progress: Progress = lambda _: None,
) -> Path:
    """Run `questions` through the agent and, unless `with_control` is off, the control arm.
    Returns the run's run.json; its report.md is beside it."""
    if not questions:
        raise RunError("no questions to run")
    lay = ingest.layout(home, pg_bin)
    if (live := discovery.find_engine(lay.app_data)) is not None:
        raise RunError(
            f"an engine (pid {live.pid}) serves {lay.app_data}, and its routes may not be "
            f"{config.name}'s: stop it (kill {live.pid}) or let it idle out, then run again"
        )
    model_dir = models_dir(home) / models.DEFAULT.key
    models.fetch(model_dir)

    started = datetime.now(UTC)
    run_id = f"{started:%Y%m%dT%H%M%SZ}-{set_name}-{config.name}"
    out = runs.runs_dir(home) / run_id
    routes_file = out / "routes.json"
    runs.write_json(routes_file, config.routes_json())
    record: runs.Record = {
        "run_id": run_id,
        "set": set_name,
        "config": config.name,
        "critic": config.critic,
        "routes": config.routes_json(),
        "questions_in_set": in_set if in_set is not None else len(questions),
        "questions": [runs.question_key(*q.key) for q in questions],
        "started_at": started.isoformat(),
        "finished_at": None,
        "stopped": None,
        "error": None,
        "engine": None,
        "utilization": {},
        "agent": [],
        "control": None,
    }

    def save() -> None:
        runs.write_json(out / "run.json", record)

    save()
    wwi = cluster.up(home, pg_bin) if any(q.source == "wwi" for q in questions) else None
    gold = sets.GoldRunner(home)
    scorer = Scorer(home, pg_bin, gold)
    try:
        engine_cmd = [
            sys.executable, "-m", "ontological.engine",
            "--app-data", str(lay.app_data), "--pg-bin", str(pg_bin),
            "--model-dir", str(model_dir), "--routes", str(routes_file), "--headless",
        ]  # fmt: skip
        asyncio.run(
            _run(home, lay, engine_cmd, config, questions, scorer, record, save, with_control,
                 progress)
        )  # fmt: skip
    except Exception as e:
        record["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        scorer.close()
        gold.close()
        if wwi is not None and wwi.action != "adopt":
            cluster.down(home, pg_bin)
        record["finished_at"] = datetime.now(UTC).isoformat()
        save()
        (out / "report.md").write_text(report.markdown(record))
    return out / "run.json"


async def _run(
    home: Path,
    lay: pg.Layout,
    engine_cmd: list[str],
    config: runs.Config,
    questions: Sequence[Question],
    scorer: Scorer,
    record: runs.Record,
    save: Callable[[], None],
    with_control: bool,
    progress: Progress,
) -> None:
    log = MemoryCallLog()
    gateway = ClaudeGateway(
        calls=log, sandbox=home / "claude-sandbox", routes={"answer_agent": config.agent}
    )
    utilization: dict[str, Any] = record["utilization"]
    utilization["start"] = await _reading(gateway)
    if gateway.status.state != "ready":
        status = gateway.status
        raise RunError(f"Claude isn't ready ({status.state}): {status.error or ''}".strip())
    save()

    await _agent_arm(lay, engine_cmd, config, questions, scorer, record, save, progress)
    utilization["after_agent"] = await _reading(gateway)
    save()
    if with_control and not record["stopped"]:
        if await _control_arm(home, gateway, log, config, questions, scorer, record, progress):
            utilization["after_control"] = await _reading(gateway)
        save()


async def _reading(gateway: ClaudeGateway) -> dict[str, Any]:
    """A ping, and the utilization per window its rate-limit event carried."""
    await gateway.check()
    limit = gateway.status.rate_limit
    return {
        "at": datetime.now(UTC).isoformat(),
        "status": limit.status if limit else None,
        "windows": dict(limit.utilization) if limit else {},
    }


async def _agent_arm(
    lay: pg.Layout,
    engine_cmd: list[str],
    config: runs.Config,
    questions: Sequence[Question],
    scorer: Scorer,
    record: runs.Record,
    save: Callable[[], None],
    progress: Progress,
) -> None:
    failed: str | None = None
    pid: int | None = None
    # Errors are kept, not raised, inside hold_engine: its task group would wrap them.
    async with hold_engine(lay.app_data, engine_cmd, client=CLIENT) as link:
        try:
            info = await link.engine()
        except EngineUnavailable as e:
            failed = f"no engine: {e}"
        else:
            pid = info.pid
            record["engine"] = {"pid": info.pid, "version": info.version}
            try:
                failed = await _ask_all(
                    lay, link, config, questions, scorer, record, save, progress
                )
            except (psycopg.Error, OSError) as e:
                failed = f"{type(e).__name__}: {e}"
    if pid is not None:
        await asyncio.to_thread(_stop_engine, pid)
    if failed is not None:
        raise RunError(failed)


async def _ask_all(
    lay: pg.Layout,
    link: Any,
    config: runs.Config,
    questions: Sequence[Question],
    scorer: Scorer,
    record: runs.Record,
    save: Callable[[], None],
    progress: Progress,
) -> str | None:
    """Ask each question in turn; an error that stops the run, or None."""
    db = await psycopg.AsyncConnection.connect(
        host=str(lay.pgsocket), port=pg.PORT, user=pg.USER, dbname=pg.DATABASE, autocommit=True
    )  # fmt: skip
    async with db:
        for n, q in enumerate(questions, 1):
            key = runs.question_key(*q.key)
            since = await _last_call_id(db)
            t0 = time.monotonic()
            r = await post_ask(link, ask_text(q), q.db, critic=config.critic)
            seconds = time.monotonic() - t0
            if isinstance(r, str):
                return f"{key}: {r}"
            if r.status_code != 200:
                hint = NO_SOURCE_HINT if r.status_code == 404 else ""
                return f"{key}: the engine answered {r.status_code}: {r.text[:300]}{hint}"
            asked = Asked.model_validate(r.json())
            calls = await _calls_since(db, since)
            sql, _ = predicted_sql(asked)
            no_answer = asked.answer is None and asked.outcome != "invalid"
            scored = await asyncio.to_thread(scorer.agent, q, sql, no_answer=no_answer)
            rec = agent_record(q, asked, calls, scored, seconds)
            record["agent"].append(rec)
            save()
            progress(f"agent [{n}/{len(questions)}] {key} ({q.db}): {_said(rec)}")
            if asked.outcome == "limited":
                record["stopped"] = f"Claude's rate limit, at {key}: {asked.error or ''}".strip()
                return None
    return None


def _said(rec: runs.Record) -> str:
    s = rec["score"]
    verdict = "match" if s["match"] else s["failure"]
    if s.get("dialect_artifact"):
        verdict += " (dialect artifact)"
    if "verified" in rec:
        verdict += ", verified" if rec["verified"] else ", unverified"
    return (
        f"{verdict}, {runs.spawns(rec['calls'])} spawns, "
        f"{runs.turns(rec['calls'])} turns, {rec['seconds']:.0f}s"
    )


async def _last_call_id(db: psycopg.AsyncConnection[tuple[Any, ...]]) -> int:
    cur = await db.execute("SELECT coalesce(max(id), 0) FROM ops.llm_call")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _calls_since(db: psycopg.AsyncConnection[tuple[Any, ...]], since: int) -> list[runs.Call]:
    cur = await db.execute(
        "SELECT " + CALL_COLUMNS + " FROM ops.llm_call WHERE id > %s ORDER BY id", (since,)
    )
    names = [d.name for d in cur.description or []]
    return [
        {k: v for k, v in zip(names, row, strict=True) if k != "id"} for row in await cur.fetchall()
    ]


def _stop_engine(pid: int) -> None:
    """SIGTERM the engine the run started, and wait for it to stop its store."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + ENGINE_STOP_S
    while discovery.pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)


async def _control_arm(
    home: Path,
    gateway: ClaudeGateway,
    log: MemoryCallLog,
    config: runs.Config,
    questions: Sequence[Question],
    scorer: Scorer,
    record: runs.Record,
    progress: Progress,
) -> int:
    """Answer the questions the control store for the agent's model has no answer to, and copy
    every question's answer into the run. Returns how many it asked."""
    route = config.agent
    path = runs.control_path(home, route)
    stored: dict[str, runs.Record] = runs.read_json(path) if path.exists() else {}
    todo = [q for q in questions if runs.question_key(*q.key) not in stored]
    asked = 0
    for n, q in enumerate(todo, 1):
        key = runs.question_key(*q.key)
        first = len(log.calls)
        t0 = time.monotonic()
        user = control.user_prompt(q, control.schema_dump(home, q))
        answer = await gateway.structured(
            "answer_agent", control.SYSTEM, user, control.ControlAnswer
        )
        outcome = gateway.last_outcome
        sql = answer.sql if answer else None
        no_answer = answer is None and outcome != "invalid"
        scored = await asyncio.to_thread(scorer.control, q, sql, no_answer=no_answer)
        calls = [call_dict(c) for c in log.calls[first:]]
        stored[key] = {
            **_question(q),
            "model": route.model,
            "effort": route.effort,
            "outcome": outcome,
            "error": None if answer else gateway.status.error,
            "sql": sql,
            "score": scored.to_json(),
            "calls": calls,
            "seconds": round(time.monotonic() - t0, 1),
            "run_id": record["run_id"],
        }
        runs.write_json(path, stored)
        asked += 1
        progress(f"control [{n}/{len(todo)}] {key} ({q.db}): {_said(stored[key])}")
        if outcome == "limited":
            record["stopped"] = f"Claude's rate limit, in the control arm at {key}"
            break
    record["control"] = {
        "model": route.model,
        "effort": route.effort,
        "file": str(path),
        "asked_this_run": asked,
        "records": [stored[k] for q in questions if (k := runs.question_key(*q.key)) in stored],
    }
    return asked
