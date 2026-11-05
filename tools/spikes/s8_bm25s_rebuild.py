# /// script
# requires-python = "==3.12.*"
# dependencies = ["bm25s==0.3.16", "numpy>=2"]
# ///
"""Spike S8: how long does a bm25s rebuild take at 10^5 documents?

Builds a synthetic catalog of N short texts (table and column documents:
name + synonyms + description, as ARCHITECTURE.md §7.3 indexes them), spread
over several databases. Identifiers come in the styles real catalogs use
(`cust_acct_no`, `InvoiceLineID`, `ORDER_DT`, `shipToAddr`), with glossary
abbreviations and acronyms in them.

The tokenizer is the §7.3 one: split identifiers on snake_case and camelCase,
lowercase, expand glossary acronyms, drop English stopwords. Query text goes
through the same tokenizer, so `cust_acct_no` and "customer account number"
meet in the middle.

It times:
  tokenize  texts -> token lists, the Python part of a rebuild
  index     bm25s.BM25().index(token lists)
  rebuild   tokenize + index, the full-rebuild cost the decision rule uses
  per-db    the same rebuild over one database's documents only (the
            incremental alternative)
  query     single-query retrieve(k) latency against the built index
  query during rebuild
            the same queries, run while a full rebuild runs on another thread
            and then swaps in atomically (a reference assignment), as §5.6
            describes. Shows what the rebuild's GIL use costs a search.

It also checks that the identifier-aware tokens work: planted documents must
rank first for their natural-language and identifier queries.

Decision rule (BUILD_PLAN.md, S8): a full rebuild under about 3 s means
rebuild on every change and swap atomically; slower means rebuild
incrementally per database and debounce edits.

Run from the repo root:
  uv run tools/spikes/s8_bm25s_rebuild.py [--docs 100000] [--repeats 5] [--out results.json]
"""

# Word lists are written as "a b c".split() to keep them readable.
# ruff: noqa: SIM905

from __future__ import annotations

import argparse
import json
import platform
import random
import re
import statistics
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import bm25s
import numpy as np

REBUILD_EVERY_CHANGE_THRESHOLD_S = 3.0  # BUILD_PLAN.md, S8
K = 50  # candidates per ranking fed to RRF

# Glossary (Settings -> Glossary): abbreviations and acronyms seen in
# identifiers, mapped to the words a person would type.
GLOSSARY: dict[str, tuple[str, ...]] = {
    "acct": ("account",),
    "addr": ("address",),
    "amt": ("amount",),
    "arr": ("annual", "recurring", "revenue"),
    "avg": ("average",),
    "bal": ("balance",),
    "cat": ("category",),
    "cnt": ("count",),
    "cust": ("customer",),
    "curr": ("currency",),
    "desc": ("description",),
    "dept": ("department",),
    "dob": ("date", "birth"),
    "dt": ("date",),
    "emp": ("employee",),
    "gmv": ("gross", "merchandise", "value"),
    "inv": ("invoice",),
    "ltv": ("lifetime", "value"),
    "mrr": ("monthly", "recurring", "revenue"),
    "no": ("number",),
    "num": ("number",),
    "ord": ("order",),
    "pct": ("percent",),
    "po": ("purchase", "order"),
    "prod": ("product",),
    "qty": ("quantity",),
    "rev": ("revenue",),
    "sku": ("stock", "keeping", "unit"),
    "ts": ("timestamp",),
    "txn": ("transaction",),
    "uom": ("unit", "measure"),
    "usr": ("user",),
    "wh": ("warehouse",),
}

# A small English stopword list; bm25s ships a longer one, but the engine
# tokenizes itself, so the list is explicit here.
STOPWORDS = frozenset(
    "a an and are as at be by for from has in is it its of on or that the this to was "
    "were will with which each per all any one when where who how not".split()
)

# Words: runs of letters/digits, so `_`, `.`, `-` and spaces split.
_WORD = re.compile(r"[A-Za-z0-9]+")
# camelCase / PascalCase parts: `InvoiceLineID` -> Invoice, Line, ID;
# `HTTPServer` -> HTTP, Server; `addressLine2` -> address, Line, 2.
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def tokenize(text: str) -> list[str]:
    """Identifier-aware tokens (§7.3). Glossary expansion runs before the
    stopword check, so `no` in `cust_acct_no` becomes "number"."""
    out: list[str] = []
    for word in _WORD.findall(text):
        for part in _CAMEL.findall(word):
            low = part.lower()
            expansion = GLOSSARY.get(low)
            if expansion is not None:
                out.extend(expansion)
            elif low not in STOPWORDS:
                out.append(low)
    return out


# --- synthetic catalog -------------------------------------------------------

# Concepts: (full word, abbreviations seen in identifiers).
CONCEPTS: list[tuple[str, tuple[str, ...]]] = [
    (full, tuple(abbr for abbr, exp in GLOSSARY.items() if exp == (full,)))
    for full in (
        "account address amount average balance category count customer currency "
        "description department date employee invoice number order percent product "
        "quantity revenue timestamp transaction user warehouse"
    ).split()
] + [
    (w, ())
    for w in (
        "id name code status type line item price cost discount tax total net gross "
        "region country city state postal phone email first last created updated "
        "deleted shipped billed paid due start end open close flag score rank "
        "segment channel campaign source medium session event page device browser "
        "store vendor supplier carrier shipment return refund payment method card "
        "plan subscription tier contract renewal churn lead opportunity stage owner "
        "team manager title salary hire term leave shift hour week month year quarter"
    ).split()
]
ACRONYM_CONCEPTS = [abbr for abbr, exp in GLOSSARY.items() if len(exp) > 1]

DESCRIPTION_TEMPLATES = (
    "The {a} of the {b} at the time the {c} was recorded.",
    "{A} for each {b}, in the {c} {d}.",
    "Identifier of the {a} that owns this {b}.",
    "{A} {b} as reported by the {c} system, before {d} adjustments.",
    "When the {a} {b} changed; null until the first {c}.",
    "{A} {b} code, see the {c} {d} lookup table.",
)


def _syllable_words(rng: random.Random, n: int) -> list[str]:
    """A long tail of made-up words, standing in for product names, internal
    jargon and codes that make a real catalog's vocabulary large."""
    onsets = "b c d f g h j k l m n p r s t v w z br cl dr fl gr pl st tr".split()
    vowels = "a e i o u ai ea oo".split()
    codas = ["", "n", "r", "s", "t", "x", "lk", "rd", "nt"]
    words: set[str] = set()
    while len(words) < n:
        syl = rng.randint(2, 3)
        words.add(
            "".join(rng.choice(onsets) + rng.choice(vowels) + rng.choice(codas) for _ in range(syl))
        )
    return sorted(words)


@dataclass(frozen=True)
class Doc:
    database: str
    kind: str  # "table" | "column"
    name: str
    text: str


def _render_identifier(rng: random.Random, parts: Sequence[tuple[str, tuple[str, ...]]]) -> str:
    words = [rng.choice(abbrs) if abbrs and rng.random() < 0.6 else full for full, abbrs in parts]
    style = rng.random()
    if style < 0.45:
        return "_".join(words)
    if style < 0.65:
        return words[0] + "".join(w.capitalize() for w in words[1:])
    if style < 0.85:
        return "".join("ID" if w == "id" else w.capitalize() for w in words)
    return "_".join(words).upper()


def make_catalog(n_docs: int, n_databases: int, seed: int) -> list[Doc]:
    rng = random.Random(seed)
    tail = _syllable_words(rng, 30_000)
    # Zipf-ish weights over the tail, so a few made-up words are common and
    # most are rare, as in a real vocabulary.
    tail_weights = [1.0 / (i + 1) for i in range(len(tail))]
    databases = [f"db{i:02d}_{rng.choice(tail)}" for i in range(n_databases)]
    docs: list[Doc] = []
    columns_per_table = 19  # ~5% table documents, ~95% column documents

    def words(k: int) -> list[str]:
        concepts = [rng.choice(CONCEPTS)[0] for _ in range(k)]
        return [rng.choices(tail, tail_weights)[0] if rng.random() < 0.25 else c for c in concepts]

    def sentence() -> str:
        a, b, c, d = words(4)
        return rng.choice(DESCRIPTION_TEMPLATES).format(a=a, A=a.capitalize(), b=b, c=c, d=d)

    def description() -> str:
        # One to three sentences, as compile writes them.
        text = " ".join(sentence() for _ in range(rng.randint(1, 3)))
        if rng.random() < 0.15:
            text += f" Includes {rng.choice(ACRONYM_CONCEPTS).upper()}."
        return text

    def synonyms() -> str:
        return " ".join(" ".join(words(rng.randint(1, 2))) for _ in range(rng.randint(0, 2)))

    while len(docs) < n_docs:
        db = rng.choice(databases)
        table_parts = [rng.choice(CONCEPTS) for _ in range(rng.randint(1, 3))]
        table = _render_identifier(rng, table_parts)
        schema = rng.choice(("public", "sales", "finance", "ops", "hr", "marketing", "raw"))
        docs.append(
            Doc(db, "table", f"{schema}.{table}", f"{schema}.{table} {synonyms()} {description()}")
        )
        for _ in range(columns_per_table):
            if len(docs) >= n_docs:
                break
            col = _render_identifier(rng, [rng.choice(CONCEPTS) for _ in range(rng.randint(1, 4))])
            docs.append(
                Doc(db, "column", f"{table}.{col}", f"{table}.{col} {synonyms()} {description()}")
            )
    return docs


# Planted documents and the queries that must find them first. They check the
# tokenizer end to end: abbreviations, camelCase with a trailing acronym,
# UPPER_SNAKE, and an acronym query against expanded words.
PLANTED: list[tuple[Doc, list[str]]] = [
    (
        Doc("db_planted", "column", "billing_master.cust_acct_no", "billing_master.cust_acct_no"),
        ["customer account number", "cust_acct_no", "billing master cust acct no"],
    ),
    (
        Doc(
            "db_planted",
            "column",
            "zorblax_line.ZorblaxInvoiceLineID",
            "zorblax_line.ZorblaxInvoiceLineID Zorblax invoice line identifier",
        ),
        ["zorblax invoice line id", "ZorblaxInvoiceLineID"],
    ),
    (
        Doc(
            "db_planted",
            "column",
            "quuxtrel_items.QUUXTREL_SKU_QTY",
            "quuxtrel_items.QUUXTREL_SKU_QTY Units on hand.",
        ),
        ["quuxtrel stock keeping unit quantity", "quuxtrel SKU qty"],
    ),
]


def make_queries(rng: random.Random, n: int) -> list[str]:
    """Queries shaped like search_ontology calls and plan() entities: a few
    business words, sometimes an identifier, sometimes an acronym."""
    out: list[str] = []
    for _ in range(n):
        r = rng.random()
        parts = [rng.choice(CONCEPTS) for _ in range(rng.randint(1, 3))]
        if r < 0.5:
            out.append(" ".join(full for full, _ in parts))
        elif r < 0.8:
            out.append(_render_identifier(rng, parts))
        else:
            out.append(f"{rng.choice(ACRONYM_CONCEPTS).upper()} by {parts[0][0]}")
    return out


# --- measurement -------------------------------------------------------------


def build_index(token_lists: list[list[str]]) -> bm25s.BM25:
    retriever = bm25s.BM25()
    retriever.index(token_lists, show_progress=False)
    return retriever


def rebuild(texts: Iterable[str]) -> bm25s.BM25:
    return build_index([tokenize(t) for t in texts])


def search(retriever: bm25s.BM25, query: str, k: int) -> np.ndarray:
    tokens = tokenize(query) or [""]
    k = min(k, retriever.scores["num_docs"])
    ids, _scores = retriever.retrieve([tokens], k=k, show_progress=False, n_threads=0)
    return ids[0]


def timed[T](fn: Callable[[], T]) -> tuple[T, float]:
    t0 = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - t0


def summarize(xs: Sequence[float]) -> dict[str, float]:
    s = sorted(xs)

    def pct(p: float) -> float:
        return s[min(len(s) - 1, round(p * (len(s) - 1)))]

    return {
        "n": len(s),
        "min": s[0],
        "p50": statistics.median(s),
        "p95": pct(0.95),
        "max": s[-1],
    }


class IndexHolder:
    """The atomic swap: readers take `current` once per search; a rebuild
    assigns a whole new index. Assigning an attribute is atomic in CPython,
    so a search sees the old index or the new one, never a half-built one."""

    def __init__(self, retriever: bm25s.BM25) -> None:
        self.current = retriever


def queries_during_rebuild(
    holder: IndexHolder, texts: list[str], queries: list[str]
) -> tuple[list[float], float, int]:
    """Runs a full rebuild on a thread and searches in a loop until it swaps.
    Returns per-query latencies, the rebuild's wall time, and how many
    searches ran against the new index after the swap (should be 0 or 1)."""
    old = holder.current
    done = threading.Event()
    rebuild_wall: list[float] = []

    def worker() -> None:
        new, dt = timed(lambda: rebuild(texts))
        holder.current = new
        rebuild_wall.append(dt)
        done.set()

    latencies: list[float] = []
    after_swap = 0
    thread = threading.Thread(target=worker)
    thread.start()
    i = 0
    while not done.is_set():
        retriever = holder.current
        query = queries[i % len(queries)]
        _, dt = timed(lambda r=retriever, q=query: search(r, q, K))
        latencies.append(dt)
        if retriever is not old:
            after_swap += 1
        i += 1
    thread.join()
    return latencies, rebuild_wall[0], after_swap


def check_planted(retriever: bm25s.BM25, docs: list[Doc]) -> list[dict[str, Any]]:
    index_of = {d.name: i for i, d in enumerate(docs)}
    results: list[dict[str, Any]] = []
    for doc, queries in PLANTED:
        for q in queries:
            ids = search(retriever, q, 5)
            top = [docs[int(i)].name for i in ids]
            results.append(
                {
                    "doc": doc.name,
                    "query": q,
                    "tokens": tokenize(q),
                    "rank": top.index(doc.name) + 1 if doc.name in top else None,
                    "top1": top[0],
                }
            )
            assert int(ids[0]) == index_of[doc.name], f"{q!r} ranked {top}"
    return results


def environment() -> dict[str, str]:
    try:
        cpu = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        cpu = platform.processor()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu": cpu,
        "bm25s": bm25s.__version__,
        "numpy": np.__version__,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--docs", type=int, default=100_000)
    ap.add_argument("--databases", type=int, default=10)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--queries", type=int, default=1_000)
    ap.add_argument("--seed", type=int, default=8)
    ap.add_argument("--out", help="write the full results as JSON here")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    docs = make_catalog(args.docs - len(PLANTED), args.databases, args.seed)
    docs += [d for d, _ in PLANTED]
    texts = [d.text for d in docs]
    queries = make_queries(rng, args.queries)

    # Corpus shape.
    token_lists = [tokenize(t) for t in texts]
    lengths = [len(t) for t in token_lists]
    vocab = {tok for toks in token_lists for tok in toks}
    by_db: dict[str, list[str]] = {}
    for d in docs:
        by_db.setdefault(d.database, []).append(d.text)
    biggest_db = max(by_db, key=lambda k: len(by_db[k]))

    # Warm-up: first-call costs (imports, allocator) stay out of the numbers.
    rebuild(texts[:1_000])

    tok_s: list[float] = []
    idx_s: list[float] = []
    rebuild_s: list[float] = []
    perdb_s: list[float] = []
    retriever = None
    for _ in range(args.repeats):
        toks, t_tok = timed(lambda: [tokenize(t) for t in texts])
        retriever, t_idx = timed(lambda toks=toks: build_index(toks))
        tok_s.append(t_tok)
        idx_s.append(t_idx)
        _, t_full = timed(lambda: rebuild(texts))
        rebuild_s.append(t_full)
        _, t_db = timed(lambda: rebuild(by_db[biggest_db]))
        perdb_s.append(t_db)
    assert retriever is not None

    planted = check_planted(retriever, docs)

    for q in queries[:50]:  # warm-up
        search(retriever, q, K)
    query_s = [timed(lambda q=q: search(retriever, q, K))[1] for q in queries]

    holder = IndexHolder(retriever)
    during_s, during_rebuild_wall, after_swap = queries_during_rebuild(holder, texts, queries)
    assert holder.current is not retriever

    index_bytes = sum(v.nbytes for v in retriever.scores.values() if isinstance(v, np.ndarray))

    results: dict[str, Any] = {
        "env": environment(),
        "corpus": {
            "docs": len(docs),
            "databases": len(by_db),
            "biggest_db_docs": len(by_db[biggest_db]),
            "tokens_per_doc": summarize([float(x) for x in lengths]),
            "total_tokens": sum(lengths),
            "vocab": len(vocab),
            "index_mb": index_bytes / 1e6,
            "sample_texts": texts[:3],
            "sample_tokens": token_lists[:3],
        },
        "tokenize_s": summarize(tok_s),
        "index_s": summarize(idx_s),
        "rebuild_s": summarize(rebuild_s),
        "per_db_rebuild_s": summarize(perdb_s),
        "query_s": summarize(query_s),
        "query_during_rebuild_s": summarize(during_s),
        "rebuild_on_thread_s": during_rebuild_wall,
        "searches_after_swap": after_swap,
        "planted": planted,
    }
    rebuild_p50 = results["rebuild_s"]["p50"]
    rebuild_max = results["rebuild_s"]["max"]
    results["decision"] = (
        "rebuild on every change, swap atomically"
        if rebuild_max < REBUILD_EVERY_CHANGE_THRESHOLD_S
        else "rebuild incrementally per database, debounce edits"
    )

    c = results["corpus"]
    print(f"env: {results['env']}")
    print(
        f"corpus: {c['docs']:,} docs in {c['databases']} databases "
        f"(biggest {c['biggest_db_docs']:,}), {c['total_tokens']:,} tokens, "
        f"{c['tokens_per_doc']['p50']:.0f} tokens/doc p50, vocab {c['vocab']:,}, "
        f"index {c['index_mb']:.1f} MB"
    )
    for t, toks in zip(c["sample_texts"], c["sample_tokens"], strict=True):
        print(f"  {t!r}\n    -> {toks}")
    print(f"\n{'':24}{'min':>9}{'p50':>9}{'p95':>9}{'max':>9}   (n)")
    for key, label, scale, unit in (
        ("tokenize_s", "tokenize", 1, "s"),
        ("index_s", "index", 1, "s"),
        ("rebuild_s", "full rebuild", 1, "s"),
        ("per_db_rebuild_s", "per-db rebuild", 1, "s"),
        ("query_s", f"query k={K}", 1e3, "ms"),
        ("query_during_rebuild_s", "query during rebuild", 1e3, "ms"),
    ):
        s = results[key]
        row = "".join(f"{s[p] * scale:>9.3f}" for p in ("min", "p50", "p95", "max"))
        print(f"{label + ' (' + unit + ')':24}{row}   ({s['n']})")
    print(
        f"rebuild on a thread while searching: {during_rebuild_wall:.3f} s, "
        f"{after_swap} search(es) saw the new index"
    )
    print("\nplanted documents (rank of the planted doc in the top 5):")
    for p in planted:
        print(f"  {p['query']!r:42} -> {p['tokens']}  rank {p['rank']}")
    print(
        f"\nfull rebuild p50 {rebuild_p50:.3f} s, max {rebuild_max:.3f} s "
        f"(threshold {REBUILD_EVERY_CHANGE_THRESHOLD_S} s): {results['decision']}"
    )
    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
