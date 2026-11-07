"""The coverage distance on extracted entities (BUILD_PLAN.md M4.4).

M3.9's retrieval benchmark calibrated each model's `coverage_distance` on the raw question's
nearest attribute. `check_answerable` scores the entities its `answerable_extract` call pulls out
of the question, which are shorter and sit further from their attributes, so M4.4 moved each
spec's value to this calibration. A rerun shows whether the specs still match it.

Each question of a set goes through the engine's own extraction (`ontological.answer.answerable
.extract`), one Haiku call per question on the eval store's gateway, cached in its
`ops.llm_call` like a compile's, so a second run makes no call. Then, for each model, every
entity's nearest attribute (dense, as `check_answerable` scores it) in its own database is a
positive and in each other eval database a negative, and Youden's J picks the distance, as
`retrieval.calibrate` does for questions. The question-level rows say what `check_answerable`
would return: each question's coverage in its own database and in each other one, at the model's
spec value, at the entity-calibrated one, and at upstream's 0.75.

It reuses the retrieval benchmark's semantic collections (`vdb.bench_semantic_<model>`), rebuilt
from the store, so it needs a compile of the eval store and touches nothing the engine reads.
"""

# The vendored vdb is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import json
import statistics
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from auto_ontology.semantic.constants import LABEL_COLUMN_ATTRIBUTE
from ontological.answer.answerable import extract
from ontological.embed import model as models
from ontological.embed.embedder import LocalEmbedder
from ontological.engine import discovery
from ontological.llm.calls import PgCallLog
from ontological.llm.gateway import ClaudeGateway
from ontological.store import dal, pg

from . import questions, retrieval, sets
from .home import app_data_dir, models_dir, results_dir
from .questions import Question
from .retrieval import UPSTREAM_THRESHOLD, BenchmarkError, Threshold, calibrate


@dataclass(frozen=True)
class Extracted:
    source: str
    id: int
    db: str
    question: str
    sanitized: str
    entities: list[str]
    extracted: bool


@dataclass(frozen=True)
class AtThreshold:
    """What `check_answerable` returns at one distance, over the set."""

    value: float
    entity_tpr: float  # own-database entities covered
    entity_fpr: float  # other-database entity pairs covered
    own_mean: float  # mean coverage of a question in its own database
    other_mean: float  # in each other database
    own_full: float  # questions fully covered in their own database
    other_full: float  # question-database pairs fully covered elsewhere


@dataclass
class ModelResult:
    model: str
    spec_value: float  # the spec's coverage_distance
    threshold: Threshold  # calibrated on entities
    at: list[AtThreshold]  # at the spec's value, the entity-calibrated one and upstream's
    # (source, id) -> database -> each entity's nearest distance, None when it has no row there
    nearest: dict[str, dict[str, list[float | None]]]


@dataclass
class Report:
    questions: int
    entities: int
    failed: int  # extractions that fell back to the question
    extracted: list[Extracted]
    models: list[ModelResult]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _key(e: Extracted) -> str:
    return f"{e.source}:{e.id}"


def covered(nearest: Sequence[float | None], t: float) -> float:
    """A question's coverage at `t`: the fraction of its entities with an attribute that near."""
    return sum(d is not None and d <= t for d in nearest) / len(nearest) if nearest else 0.0


def at_threshold(
    t: float,
    extracted: Sequence[Extracted],
    nearest: dict[str, dict[str, list[float | None]]],
) -> AtThreshold:
    own: list[float] = []
    other: list[float] = []
    ent_own: list[bool] = []
    ent_other: list[bool] = []
    for e in extracted:
        for database, distances in nearest[_key(e)].items():
            hits = [d is not None and d <= t for d in distances]
            (ent_own if database == e.db else ent_other).extend(hits)
            (own if database == e.db else other).append(covered(distances, t))
    return AtThreshold(
        value=round(t, 4),
        entity_tpr=round(statistics.fmean(ent_own), 4),
        entity_fpr=round(statistics.fmean(ent_other), 4),
        own_mean=round(statistics.fmean(own), 4),
        other_mean=round(statistics.fmean(other), 4),
        own_full=round(statistics.fmean(c == 1.0 for c in own), 4),
        other_full=round(statistics.fmean(c == 1.0 for c in other), 4),
    )


async def extract_all(gateway: ClaudeGateway, chosen: Sequence[Question]) -> list[Extracted]:
    async def one(q: Question) -> Extracted:
        sanitized, entities, ok = await extract(gateway, q.question)
        return Extracted(q.source, q.id, q.db, q.question, sanitized, entities, ok)

    return list(await asyncio.gather(*(one(q) for q in chosen)))


async def _ready_then_extract(
    gateway: ClaudeGateway, chosen: Sequence[Question]
) -> list[Extracted]:
    status = await gateway.check()
    if status.state != "ready":
        raise BenchmarkError(f"Claude isn't ready ({status.state}): {status.error or ''}".strip())
    return await extract_all(gateway, chosen)


def score(
    extracted: Sequence[Extracted],
    specs: Sequence[models.ModelSpec],
    model_dir: Callable[[models.ModelSpec], Path],
    *,
    progress: Callable[[str], None] = lambda _: None,
) -> list[ModelResult]:
    """Each model's entity distances and thresholds. The store must be connected and compiled."""
    databases = sorted({e.db for e in extracted})
    if len(databases) < 2:
        raise BenchmarkError("calibration needs questions from at least two databases")
    rows = retrieval.layer_rows("semantic", databases)
    if not rows:
        raise BenchmarkError("the semantic layer is empty: compile the eval store first")
    results: list[ModelResult] = []
    for spec in specs:
        progress(f"{spec.key}: embedding {len(rows)} semantic-layer rows")
        embedder = LocalEmbedder(model_dir(spec), spec=spec)
        embedder.calibrate()
        vdb = retrieval.build_collection("semantic", embedder, rows)
        try:
            nearest: dict[str, dict[str, list[float | None]]] = {}
            positives: list[float] = []
            negatives: list[float] = []
            for e in extracted:
                by_db = nearest.setdefault(_key(e), {})
                for database in databases:
                    ds: list[float | None] = []
                    for entity in e.entities:
                        d = retrieval.nearest_distance(
                            vdb, entity, LABEL_COLUMN_ATTRIBUTE, database
                        )
                        ds.append(None if d is None else round(d, 4))
                        if d is not None:
                            (positives if database == e.db else negatives).append(round(d, 4))
                    by_db[database] = ds
        finally:
            vdb.close()
        threshold = calibrate(positives, negatives)
        values = (spec.coverage_distance, threshold.value, UPSTREAM_THRESHOLD)
        results.append(
            ModelResult(
                model=spec.key,
                spec_value=spec.coverage_distance,
                threshold=threshold,
                at=[at_threshold(t, extracted, nearest) for t in values],
                nearest=nearest,
            )
        )
    return results


def markdown(report: Report) -> str:
    lines = [
        f"{report.questions} questions, {report.entities} entities "
        f"({report.entities / report.questions:.1f} a question); "
        f"{report.failed} extraction(s) fell back to the question.",
        "",
        "**Entity threshold** (dense, nearest attribute; own database vs other databases)",
        "",
        "| model | spec | entities | TPR | FPR | own p50 | other p50 |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in report.models:
        t = m.threshold
        lines.append(
            f"| {m.model} | {m.spec_value:.3f} | {t.value:.3f} | {t.tpr:.3f} | {t.fpr:.3f} | "
            f"{statistics.median(t.positives):.3f} | {statistics.median(t.negatives):.3f} |"
        )
    lines += [
        "",
        "**What check_answerable returns** (coverage per question; own database vs others)",
        "",
        "| model | at | entity TPR | entity FPR | own mean | other mean | own full | other full |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in report.models:
        for a in m.at:
            lines.append(
                f"| {m.model} | {a.value:.3f} | {a.entity_tpr:.3f} | {a.entity_fpr:.3f} | "
                f"{a.own_mean:.3f} | {a.other_mean:.3f} | {a.own_full:.3f} | "
                f"{a.other_full:.3f} |"
            )
    lines.append("")
    return "\n".join(lines)


def bench(
    home: Path,
    pg_bin: Path,
    set_name: str,
    keys: Sequence[str],
    *,
    progress: Callable[[str], None] = lambda _: None,
) -> tuple[Report, Path]:
    """Extract every question of one frozen set's entities (Claude quota: one Haiku call a
    question, none on a rerun), score them for each model, and write the report to
    <home>/results/answerable-<set>.json. Starts the store's Postgres if it isn't running, and
    stops it again if this started it."""
    specs = [models.CANDIDATES[k] for k in keys]
    for spec in specs:
        progress(f"{spec.key}: checking {spec.id}")
        models.fetch(models_dir(home) / spec.key, spec)
    by_key = questions.load(home)
    chosen = [by_key[k] for k in sets.read()[set_name]]
    lay = pg.Layout(app_data_dir(home), pg_bin)
    with discovery.engine_lock(lay.app_data):
        if (live := discovery.find_engine(lay.app_data)) is not None:
            raise BenchmarkError(f"an engine (pid {live.pid}) serves {lay.app_data}: stop it first")
        server = pg.ensure_running(lay)
        try:
            pg.migrate(lay)
            dal.connect(lay)
            try:
                gateway = ClaudeGateway(
                    calls=PgCallLog(pg.url(lay)), sandbox=lay.app_data / "claude-sandbox"
                )
                gateway.sandbox.mkdir(parents=True, exist_ok=True)
                progress(f"extracting the entities of {len(chosen)} questions")
                extracted = asyncio.run(_ready_then_extract(gateway, chosen))
                if all(not e.extracted for e in extracted):
                    raise BenchmarkError("every extraction failed: is Claude logged in?")
                results = score(
                    extracted, specs, lambda s: models_dir(home) / s.key, progress=progress
                )
            finally:
                dal.close()
        finally:
            if server.action != "adopt":
                pg.stop(lay)
    report = Report(
        questions=len(extracted),
        entities=sum(len(e.entities) for e in extracted),
        failed=sum(not e.extracted for e in extracted),
        extracted=extracted,
        models=results,
    )
    out = results_dir(home) / f"answerable-{set_name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_json())
    return report, out
