"""The frozen question sets, eval/sets/{dev,heldout,smoke}.json (BUILD_PLAN.md, Eval set).

Each file holds question IDs only, by source: {"wwi": [...], "bird": [...]}. `freeze` chooses
them from the downloaded data with a fixed seed:

    dev       every WWI question but WWI_DROPPED, and 10 BIRD questions from each of the four
              databases
    heldout   40 more BIRD questions, 10 per database where it has them; debit_card_specializing
              has 14 in all, so it gives 4 and the other three 12
    smoke     10 WWI and 10 BIRD questions from dev

Every BIRD draw follows Mini-Dev's own simple / moderate / challenging mix, counted over all 500
questions: the rarest difficulties are placed first, then the rest fill each database's quota.

`check` runs each set's gold SQL natively, sqlite3 for BIRD and the eval cluster for WWI, and
reports every query that fails or returns no rows. Once M0 ends nothing in eval/sets/ changes
unless a question is broken, and then as a commit of its own that says why.
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
from ontological.store import pg

from . import cluster
from .home import bird_dir, cluster_dir
from .questions import BIRD_DATABASES, WWI_DROPPED, Question, Source, load_bird, load_wwi

SEED = 808
SETS_DIR = Path(__file__).resolve().parent.parent / "sets"
NAMES = ("dev", "heldout", "smoke")
DEV_PER_DATABASE = 10
HELDOUT_BIRD = 40
SMOKE_WWI = 10
SMOKE_BIRD = 10

Key = tuple[Source, int]
Sets = dict[str, list[Key]]
Rows = Sequence[Sequence[Any]]


class SetsError(RuntimeError):
    pass


# --- choosing -----------------------------------------------------------------------------------


def apportion(weights: Mapping[str, int], n: int) -> dict[str, int]:
    """Split n in proportion to weights by largest remainder; ties go to the earlier key."""
    total = sum(weights.values())
    exact = {k: n * w / total for k, w in weights.items()}
    out = {k: int(v) for k, v in exact.items()}
    by_remainder = sorted(weights, key=lambda k: out[k] - exact[k])  # stable: keeps key order
    for k in by_remainder[: n - sum(out.values())]:
        out[k] += 1
    return out


def spread(n: int, capacity: Mapping[str, int]) -> dict[str, int]:
    """Split n as evenly as capacity allows. What a full key can't take goes to the others, and
    the last few go to the earlier keys."""
    if n > sum(capacity.values()):
        raise SetsError(f"can't take {n} questions from {dict(capacity)}")
    out = dict.fromkeys(capacity, 0)
    left = n
    while left:
        open_ = [k for k in capacity if out[k] < capacity[k]]
        share = max(1, left // len(open_))
        for k in open_:
            add = min(share, capacity[k] - out[k], left)
            out[k] += add
            left -= add
    return out


def sample(
    pool: Iterable[Question], quotas: Mapping[str, int], mix: Mapping[str, int], rng: random.Random
) -> list[Question]:
    """quotas[db] questions from each database, in mix's proportions of difficulty overall."""
    shuffled = sorted(pool, key=lambda q: q.key)
    rng.shuffle(shuffled)
    left = dict(quotas)
    chosen: dict[Key, Question] = {}

    def take(q: Question) -> None:
        chosen[q.key] = q
        left[q.db] -= 1

    for difficulty, want in sorted(
        apportion(mix, sum(quotas.values())).items(), key=lambda kv: mix[kv[0]]
    ):
        for q in shuffled:
            if want and q.difficulty == difficulty and left.get(q.db, 0) and q.key not in chosen:
                take(q)
                want -= 1
    # A database short of some difficulty fills its quota from the others.
    for q in shuffled:
        if left.get(q.db, 0) and q.key not in chosen:
            take(q)
    if short := {db: n for db, n in left.items() if n}:
        raise SetsError(f"not enough questions to fill {short}")
    return list(chosen.values())


def per_database(questions: Iterable[Question]) -> dict[str, int]:
    counts = Counter(q.db for q in questions)
    return {db: counts[db] for db in BIRD_DATABASES}


def choose(wwi: Sequence[Question], bird: Sequence[Question], seed: int = SEED) -> Sets:
    """The three sets, from every WWI question and every Mini-Dev question."""
    rng = random.Random(seed)
    mix = Counter(q.difficulty for q in bird)
    pool = [q for q in bird if q.db in BIRD_DATABASES]
    wwi_ids = sorted(q.id for q in wwi if q.id not in WWI_DROPPED)

    dev_bird = sample(pool, dict.fromkeys(BIRD_DATABASES, DEV_PER_DATABASE), mix, rng)
    taken = {q.key for q in dev_bird}
    rest = [q for q in pool if q.key not in taken]
    heldout = sample(rest, spread(HELDOUT_BIRD, per_database(rest)), mix, rng)
    smoke_bird = sample(dev_bird, spread(SMOKE_BIRD, per_database(dev_bird)), mix, rng)
    smoke_wwi = rng.sample(wwi_ids, SMOKE_WWI)

    def keys(wwi_part: Iterable[int], bird_part: Iterable[Question]) -> list[Key]:
        return [*(("wwi", i) for i in sorted(wwi_part)), *sorted(q.key for q in bird_part)]

    return {
        "dev": keys(wwi_ids, dev_bird),
        "heldout": keys([], heldout),
        "smoke": keys(smoke_wwi, smoke_bird),
    }


# --- the files ----------------------------------------------------------------------------------


def to_json(keys: Iterable[Key]) -> str:
    by_source: dict[str, list[int]] = {"wwi": [], "bird": []}
    for source, i in keys:
        by_source[source].append(i)
    return json.dumps({s: sorted(ids) for s, ids in by_source.items()}, indent=2) + "\n"


def write(sets: Sets, directory: Path = SETS_DIR, *, force: bool = False) -> list[Path]:
    paths = [directory / f"{name}.json" for name in NAMES]
    if not force and (existing := [p for p in paths if p.exists()]):
        raise SetsError(
            f"{', '.join(p.name for p in existing)} already exist and the sets are frozen. "
            "Pass --force only to rewrite them before M0 ends."
        )
    directory.mkdir(parents=True, exist_ok=True)
    for name, path in zip(NAMES, paths, strict=True):
        path.write_text(to_json(sets[name]))
    return paths


def read(directory: Path = SETS_DIR) -> Sets:
    out: Sets = {}
    for name in NAMES:
        raw: dict[str, list[int]] = json.loads((directory / f"{name}.json").read_text())
        wwi: list[Key] = [("wwi", i) for i in raw["wwi"]]
        bird: list[Key] = [("bird", i) for i in raw["bird"]]
        out[name] = wwi + bird
    return out


def freeze(home: Path, directory: Path = SETS_DIR, *, force: bool = False) -> Sets:
    sets = choose(load_wwi(home), load_bird(home))
    write(sets, directory, force=force)
    return sets


# --- checking -----------------------------------------------------------------------------------


def shape_problems(sets: Sets, questions: Mapping[Key, Question]) -> list[str]:
    """IDs that don't exist or were dropped, overlap between dev and held-out, and smoke
    questions that aren't in dev."""
    out: list[str] = []
    for name, keys in sets.items():
        if len(set(keys)) != len(keys):
            out.append(f"{name}: lists a question twice")
        for key in keys:
            q = questions.get(key)
            if q is None:
                out.append(f"{name}: {key[0]} {key[1]} isn't in the downloaded questions")
            elif q.source == "wwi" and q.id in WWI_DROPPED:
                out.append(f"{name}: wwi {q.id} was dropped: {WWI_DROPPED[q.id]}")
            elif q.source == "bird" and q.db not in BIRD_DATABASES:
                out.append(f"{name}: bird {q.id} is from {q.db}, not one of the four databases")
    if both := set(sets["dev"]) & set(sets["heldout"]):
        out.append(f"dev and heldout share {sorted(both)}")
    if extra := set(sets["smoke"]) - set(sets["dev"]):
        out.append(f"smoke has questions that aren't in dev: {sorted(extra)}")
    return out


def gold_problem(q: Question, run: Callable[[Question], Rows]) -> str | None:
    """Why q's gold SQL can't score answers, or None: it must run and return at least one row
    that isn't all NULL (an aggregate over nothing returns a single NULL row)."""
    try:
        rows = run(q)
    except Exception as e:
        return f"{q.source} {q.id}: fails: {str(e).strip().splitlines()[0]}"
    if not any(any(v is not None for v in row) for row in rows):
        return f"{q.source} {q.id}: returns " + ("only NULLs" if rows else "no rows")
    return None


class GoldRunner:
    """Runs gold SQL where it lives: BIRD on its .sqlite file through sqlite3, opened read-only,
    and WWI on the eval cluster in read-only transactions."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self._sqlite: dict[str, sqlite3.Connection] = {}
        self._pg: psycopg.Connection[tuple[Any, ...]] | None = None

    def __call__(self, q: Question) -> Rows:
        return self._run_wwi(q.sql) if q.source == "wwi" else self._run_bird(q.db, q.sql)

    def _run_bird(self, db: str, sql: str) -> Rows:
        if db not in self._sqlite:
            path = bird_dir(self.home) / f"{db}.sqlite"
            if not path.exists():
                raise SetsError(f"{path} is missing: run `fetch bird-databases`")
            self._sqlite[db] = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        return self._sqlite[db].execute(sql).fetchall()

    def _run_wwi(self, sql: str) -> Rows:
        if self._pg is None:
            socket = cluster_dir(self.home) / "pgsocket"
            try:
                self._pg = psycopg.connect(
                    host=str(socket), port=pg.PORT, user=pg.USER, dbname=cluster.DATABASE,
                    autocommit=True,
                )  # fmt: skip
            except psycopg.OperationalError as e:
                raise SetsError(f"can't reach the eval cluster: run `cluster up` ({e})") from e
            self._pg.execute("SET statement_timeout = '60s'")
            self._pg.execute("SET default_transaction_read_only = on")
        with self._pg.cursor() as cur:
            cur.execute(sql.encode())
            return cur.fetchall() if cur.description else []

    def close(self) -> None:
        for conn in self._sqlite.values():
            conn.close()
        if self._pg is not None:
            self._pg.close()


def check(
    sets: Sets, questions: Mapping[Key, Question], run: Callable[[Question], Rows]
) -> list[str]:
    """Every problem with the sets: their shape, then each gold query (smoke is inside dev)."""
    if problems := shape_problems(sets, questions):
        return problems
    keys = sorted({k for name in NAMES for k in sets[name]})
    return [p for k in keys if (p := gold_problem(questions[k], run))]
