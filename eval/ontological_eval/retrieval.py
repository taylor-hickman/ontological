"""The retrieval benchmark (ARCHITECTURE.md §7.4, BUILD_PLAN.md M3.9).

For each question, the engine's SQL parser (`ontological.catalog.sql_parse`, the one query
history goes through) resolves the gold SQL to the catalog's tables and columns. The benchmark
then asks what retrieval found of them, for each candidate embedding model and each configuration
(dense only, BM25 only, trigram only, and hybrid), at several k. Nothing here calls Claude.

Two layers can be measured:

- `semantic`: the grounding pack (`ontological.answer.pack.build_pack`), as the agent's pre-work
  builds it from the raw question and its evidence, scoped to the question's database as the eval
  runner scopes it. k is the pack's `question_k`. Table recall is over the pack's tables; column
  recall is over its attributes' columns, counting only gold columns that have an attribute (a
  declared-FK column gets a semantic FK in place of one). It needs a compile of the eval store.
- `data`: the data layer, which ingest embeds with no compile: the question's k nearest column
  rows and the evidence's `EVIDENCE_K`, with the tables of those columns and the question's
  nearest table row. It stands in for the semantic layer's shape on the catalog's own texts.

Each model gets a collection of its own per layer (`vdb.bench_<layer>_<model>`) in the eval
store, rebuilt each run from the store's rows, so nothing the engine reads is touched.

The coverage threshold (`check_answerable`, §7.3) is recalibrated from dense distances, per
model: a question's nearest attribute in its own database is a positive, and in each other eval
database a negative, since the question can't be answered there. The threshold is the distance
that best separates them (Youden's J).
"""

# The vendored DAL and vdb are untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false

from __future__ import annotations

import itertools
import json
import logging
import statistics
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import psycopg
from auto_ontology.catalog.constants import Labels
from auto_ontology.dal import schema as s
from auto_ontology.dal.session import store
from auto_ontology.semantic.constants import LABEL_COLUMN_ATTRIBUTE
from auto_ontology.vdb import VDB_SCHEMA, get_vdb
from auto_ontology.vdb.postgres import PostgresVDB
from auto_ontology.vdb.records import content_hash, write_embedded_rows
from ontological.answer.pack import build_pack
from ontological.answer.retrieval import EVIDENCE_K, Search
from ontological.catalog.data_layer import data_layer_rows
from ontological.catalog.parsers.catalog_index import CatalogIndex
from ontological.catalog.query_history import read_catalog_index
from ontological.catalog.sql_parse import parse_query
from ontological.embed import model as models
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.hybrid import RANKINGS, HybridSearch, searcher
from ontological.embed.search import dense_search
from ontological.embed.semantic import database_rows
from ontological.engine import discovery
from ontological.store import dal, pg
from psycopg import sql
from sqlalchemy import select

from . import questions, sets
from .home import app_data_dir, models_dir, results_dir
from .questions import Question

logger = logging.getLogger(__name__)

Layer = Literal["semantic", "data"]
LAYERS: tuple[Layer, ...] = ("semantic", "data")
CONFIGS: Mapping[str, tuple[str, ...]] = {
    "dense": ("dense",),
    "bm25": ("bm25",),
    "trigram": ("trgm",),
    "hybrid": RANKINGS,
}
KS = (1, 3, 5, 10, 20)
DIALECTS = {"wwi": ("postgres",), "bird": ("sqlite",)}
# Upstream's coverage threshold (cosine distance), tuned to its Nemotron embedder.
UPSTREAM_THRESHOLD = 0.75
BM25_WAIT_S = 120.0


class BenchmarkError(RuntimeError):
    pass


# --- gold ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gold:
    tables: frozenset[str]  # catalog table ids
    columns: frozenset[str]  # catalog column ids


def gold(q: Question, index: CatalogIndex) -> Gold:
    """The catalog tables and columns `q`'s gold SQL reads."""
    parsed = parse_query(q.sql, index, DIALECTS[q.source])
    if parsed is None:
        raise BenchmarkError(f"{q.source} {q.id}: the gold SQL names no table in {q.db}")
    return Gold(parsed.table_ids, parsed.column_ids)


def column_tables(index: CatalogIndex) -> dict[str, str]:
    """Each column id's table id."""
    return {
        column_id: table.id
        for schema in index.schemas.values()
        for table in schema.tables.values()
        for column_id in table.columns.values()
    }


# --- what retrieval found -----------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    tables: frozenset[str]
    columns: frozenset[str]


def recall(found: Iterable[str], wanted: frozenset[str]) -> float | None:
    """The fraction of `wanted` in `found`; None when nothing is wanted."""
    if not wanted:
        return None
    return len(wanted & set(found)) / len(wanted)


def pack_found(vdb: PostgresVDB, q: Question, k: int, search: Search) -> Found:
    """What the grounding pack holds for `q` at `question_k = k`."""
    pack = build_pack(
        vdb, q.question, evidence=q.evidence, database=q.db, question_k=k, search=search
    )
    return Found(
        frozenset(str(t["id"]) for t in pack.tables),
        frozenset(str(a["col_id"]) for a in pack.attributes),
    )


def data_found(
    vdb: PostgresVDB, q: Question, k: int, search: Search, tables_of: Mapping[str, str]
) -> Found:
    """The data layer's analogue of the pack: the question's `k` nearest columns and the
    evidence's `EVIDENCE_K`, their tables, and the question's nearest table."""

    def ids(text: str, label: str, n: int) -> list[str]:
        hits = search(vdb, text, k=n, where={"label": label, "database_name": q.db})
        return [str(h["metadata"]["id"]) for h in hits if h.get("metadata", {}).get("id")]

    columns = ids(q.question, Labels.COLUMN, k)
    if q.evidence.strip():
        columns += ids(q.evidence, Labels.COLUMN, EVIDENCE_K)
    tables = {tables_of[c] for c in columns if c in tables_of}
    tables.update(ids(q.question, Labels.TABLE, 1))
    return Found(frozenset(tables), frozenset(columns))


# --- the collections ----------------------------------------------------------------------------


def collection_name(layer: Layer, spec: models.ModelSpec) -> str:
    return f"bench_{layer}_{spec.key.replace('-', '_')}"


def layer_rows(layer: Layer, databases: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for database in databases:
        rows += data_layer_rows(database) if layer == "data" else database_rows(database)
    return rows


def build_collection(
    layer: Layer, embedder: LocalEmbedder, rows: Sequence[dict[str, Any]]
) -> PostgresVDB:
    """A fresh collection of `rows` embedded by `embedder`, in the eval store's vdb schema."""
    name = collection_name(layer, embedder.spec)
    vdb = get_vdb(collection_name=name, embeddings=embedder, vector_size=embedder.dim)
    with psycopg.connect(vdb.connection_string) as conn:
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(VDB_SCHEMA, name)))
    texts = [row["text"] for row in rows]
    vectors = embedder.embed_documents(texts)
    write_embedded_rows(
        vdb,
        [
            {
                **row,
                "metadata": {
                    **row["metadata"],
                    "embedding": vector,
                    "model": embedder.model,
                    "content_hash": content_hash(text),
                },
            }
            for row, text, vector in zip(rows, texts, vectors, strict=True)
        ],
    )
    return vdb


# --- the threshold ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Threshold:
    value: float  # cosine distance: a hit at or under it covers the question
    tpr: float  # own-database questions covered
    fpr: float  # other-database pairs covered
    upstream_tpr: float  # the same at upstream's 0.75
    upstream_fpr: float
    positives: list[float] = field(default_factory=list[float])  # nearest distances, own database
    negatives: list[float] = field(default_factory=list[float])  # in each other database


def rates(positives: Sequence[float], negatives: Sequence[float], t: float) -> tuple[float, float]:
    tpr = sum(d <= t for d in positives) / len(positives)
    fpr = sum(d <= t for d in negatives) / len(negatives)
    return tpr, fpr


def calibrate(positives: Sequence[float], negatives: Sequence[float]) -> Threshold:
    """The distance that maximizes TPR - FPR, the midpoint between neighbouring distances; ties
    go to the larger TPR."""
    if not positives or not negatives:
        raise BenchmarkError("calibration needs own-database and other-database distances")
    points = sorted(set(positives) | set(negatives))
    candidates = [(a + b) / 2 for a, b in itertools.pairwise(points)] or points

    def score(t: float) -> tuple[float, float]:
        tpr, fpr = rates(positives, negatives, t)
        return (tpr - fpr, tpr)

    best = max(candidates, key=score)
    tpr, fpr = rates(positives, negatives, best)
    up_tpr, up_fpr = rates(positives, negatives, UPSTREAM_THRESHOLD)
    return Threshold(round(best, 4), tpr, fpr, up_tpr, up_fpr, sorted(positives), sorted(negatives))


def nearest_distance(vdb: PostgresVDB, text: str, label: str, database: str) -> float | None:
    hits = dense_search(vdb, text, k=1, where={"label": label, "database_name": database})
    return float(hits[0]["_distance"]) if hits else None


# --- the run ------------------------------------------------------------------------------------


@dataclass
class ModelResult:
    model: str
    model_id: str
    dim: int
    rows: int
    # config -> k -> {"tables": mean recall, "columns": mean recall}
    recall: dict[str, dict[int, dict[str, float]]]
    threshold: Threshold | None
    embed_seconds: float


@dataclass
class Report:
    layer: Layer
    questions: int
    scored_columns: int  # gold columns the layer has a row for, over all questions
    gold_columns: int
    ks: list[int]
    models: list[ModelResult]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _mean(values: Iterable[float | None]) -> float:
    kept = [v for v in values if v is not None]
    return statistics.fmean(kept) if kept else 0.0


def attribute_columns(databases: Sequence[str]) -> frozenset[str]:
    """Every column of `databases` that has a column attribute."""
    t = s.catalog_table.join(
        s.catalog_schema, s.catalog_schema.c.id == s.catalog_table.c.schema_id
    ).join(s.catalog_database, s.catalog_database.c.id == s.catalog_schema.c.database_id)
    rows = store().query_read(
        select(s.column__has_attribute.c.column_id)
        .select_from(
            s.column__has_attribute.join(
                s.catalog_column, s.catalog_column.c.id == s.column__has_attribute.c.column_id
            ).join(t, s.catalog_table.c.id == s.catalog_column.c.table_id)
        )
        .where(s.catalog_database.c.name.in_(list(databases)))
    )
    return frozenset(str(r["column_id"]) for r in rows)


def run(
    layer: Layer,
    questions: Sequence[Question],
    specs: Sequence[models.ModelSpec],
    model_dir: Callable[[models.ModelSpec], Path],
    *,
    ks: Sequence[int] = KS,
    configs: Mapping[str, tuple[str, ...]] = CONFIGS,
    others: Sequence[str] = (),
    progress: Callable[[str], None] = lambda _: None,
) -> Report:
    """Run the benchmark on `questions` for each model in `specs`. The store must be connected
    (`ontological.store.dal.connect`) and hold every question's database, and `others`, more
    databases that are searched and calibrated against but have no questions."""
    databases = sorted({q.db for q in questions} | set(others))
    indexes = {d: read_catalog_index(d) for d in databases}
    tables_of: dict[str, str] = {}
    for index in indexes.values():
        tables_of.update(column_tables(index))
    golds = {q.key: gold(q, indexes[q.db]) for q in questions}
    rows = layer_rows(layer, databases)
    if not rows:
        raise BenchmarkError(
            f"the {layer} layer of {', '.join(databases)} is empty"
            + (": compile the eval store first" if layer == "semantic" else ": ingest it first")
        )
    scoreable = frozenset(tables_of) if layer == "data" else attribute_columns(databases)
    wanted_columns = {key: g.columns & scoreable for key, g in golds.items()}

    results: list[ModelResult] = []
    for spec in specs:
        progress(f"{spec.key}: embedding {len(rows)} {layer}-layer rows")
        embedder = LocalEmbedder(model_dir(spec), spec=spec)
        embedder.calibrate()
        start = time.perf_counter()
        vdb = build_collection(layer, embedder, rows)
        embed_seconds = time.perf_counter() - start
        hybrid = HybridSearch(vdb)
        try:
            if not hybrid.bm25.wait(BM25_WAIT_S):
                raise BenchmarkError(f"{spec.key}: the BM25 index didn't build in {BM25_WAIT_S} s")
            recall_by: dict[str, dict[int, dict[str, float]]] = {}
            for config, rankings in configs.items():
                progress(f"{spec.key}: {config}")
                search = searcher(hybrid, rankings)
                recall_by[config] = {}
                for k in ks:
                    t_recalls: list[float | None] = []
                    c_recalls: list[float | None] = []
                    for q in questions:
                        found = (
                            pack_found(vdb, q, k, search)
                            if layer == "semantic"
                            else data_found(vdb, q, k, search, tables_of)
                        )
                        t_recalls.append(recall(found.tables, golds[q.key].tables))
                        c_recalls.append(recall(found.columns, wanted_columns[q.key]))
                    recall_by[config][k] = {
                        "tables": round(_mean(t_recalls), 4),
                        "columns": round(_mean(c_recalls), 4),
                    }
            label = LABEL_COLUMN_ATTRIBUTE if layer == "semantic" else Labels.COLUMN
            positives: list[float] = []
            negatives: list[float] = []
            for q in questions:
                for database in databases:
                    d = nearest_distance(vdb, q.question, label, database)
                    if d is not None:
                        (positives if database == q.db else negatives).append(round(d, 4))
            threshold = calibrate(positives, negatives) if len(databases) > 1 else None
        finally:
            hybrid.close()
            vdb.close()
        results.append(
            ModelResult(
                spec.key,
                spec.id,
                spec.dim,
                len(rows),
                recall_by,
                threshold,
                round(embed_seconds, 2),
            )
        )

    return Report(
        layer=layer,
        questions=len(questions),
        scored_columns=sum(len(c) for c in wanted_columns.values()),
        gold_columns=sum(len(g.columns) for g in golds.values()),
        ks=list(ks),
        models=results,
    )


# --- the report ---------------------------------------------------------------------------------


def markdown(report: Report) -> str:
    """Recall tables per model, then the thresholds."""
    lines = [
        f"Layer: {report.layer}. {report.questions} questions; column recall over "
        f"{report.scored_columns} of {report.gold_columns} gold columns.",
        "",
    ]
    for what in ("tables", "columns"):
        lines.append(f"**{what.capitalize()} recall@k**")
        lines.append("")
        header = "| model | config | " + " | ".join(f"@{k}" for k in report.ks) + " |"
        lines += [header, "|" + "---|" * (2 + len(report.ks))]
        for m in report.models:
            for config, by_k in m.recall.items():
                cells = " | ".join(f"{by_k[k][what]:.3f}" for k in report.ks)
                lines.append(f"| {m.model} | {config} | {cells} |")
        lines.append("")
    if any(m.threshold for m in report.models):
        lines += [
            "**Coverage threshold** (dense, nearest row; own database vs other databases)",
            "",
            "| model | threshold | TPR | FPR | own p50 | other p50 | TPR @0.75 | FPR @0.75 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for m in report.models:
            if (t := m.threshold) is None:
                continue
            lines.append(
                f"| {m.model} | {t.value:.3f} | {t.tpr:.3f} | {t.fpr:.3f} | "
                f"{statistics.median(t.positives):.3f} | {statistics.median(t.negatives):.3f} | "
                f"{t.upstream_tpr:.3f} | {t.upstream_fpr:.3f} |"
            )
        lines.append("")
    return "\n".join(lines)


# --- the command --------------------------------------------------------------------------------


def bench(
    home: Path,
    pg_bin: Path,
    layer: Layer,
    set_name: str,
    keys: Sequence[str],
    *,
    store: str | None = None,
    progress: Callable[[str], None] = lambda _: None,
) -> tuple[Report, Path]:
    """Run the benchmark on one frozen set against the eval store, fetching each model into
    <home>/models/<key> first, and write the report to <home>/results/retrieval-<layer>-<set>.json.
    The store's Postgres is started if it isn't running, and stopped again if this started it.

    With `store`, it runs against that compile-review store (BUILD_PLAN.md M3.10) instead, on the
    set's questions whose database the store holds, calibrating against its other databases,
    and the report's name ends in `-<store>`."""
    specs = [models.CANDIDATES[k] for k in keys]
    for spec in specs:
        progress(f"{spec.key}: checking {spec.id}")
        models.fetch(models_dir(home) / spec.key, spec)
    by_key = questions.load(home)
    chosen = [by_key[k] for k in sets.read()[set_name]]
    lay = pg.Layout(app_data_dir(home, store), pg_bin)
    with discovery.engine_lock(lay.app_data):
        if (live := discovery.find_engine(lay.app_data)) is not None:
            raise BenchmarkError(f"an engine (pid {live.pid}) serves {lay.app_data}: stop it first")
        server = pg.ensure_running(lay)
        try:
            pg.migrate(lay)
            dal.connect(lay)
            others: list[str] = []
            if store:
                held = set(pg.psql(lay, "SELECT name FROM catalog_database").splitlines())
                chosen = [q for q in chosen if q.db in held]
                others = sorted(held - {q.db for q in chosen})
            try:
                report = run(
                    layer,
                    chosen,
                    specs,
                    lambda s: models_dir(home) / s.key,
                    others=others,
                    progress=progress,
                )
            finally:
                dal.close()
        finally:
            if server.action != "adopt":
                pg.stop(lay)
    out = results_dir(home) / f"retrieval-{layer}-{set_name}{f'-{store}' if store else ''}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_json())
    return report, out
