# /// script
# requires-python = "==3.12.*"
# dependencies = [
#     "fastembed==0.9.1",
#     "huggingface-hub>=1.0",
#     "numpy>=2.0",
#     "onnxruntime==1.31.1",
#     "tokenizers==0.23.3",
# ]
# ///
"""Spike S7: load snowflake-arctic-embed-m-v1.5 int8 and measure its speed.

Run from the repo root:

    uv run tools/spikes/s7_embedder.py            # full run, 10^5 bulk texts
    uv run tools/spikes/s7_embedder.py --quick    # smoke run, 5,000 bulk texts

It downloads the model files once, at a pinned revision, into a local directory, then
loads them from there with no network, the way the bundled app will. It loads the model
through fastembed (as a custom model) and through onnxruntime + tokenizers directly, checks
that the two give the same vectors, and measures:

1. Batch time over a grid of batch size x sequence length, to find how many padded tokens
   one batch can hold and still finish within the query budget (100 ms).
2. Texts per second for a bulk run over synthetic catalog texts.
3. Single-query latency: idle; behind a priority queue whose one worker runs bulk batches
   (a query waits for the running batch, then goes next); and concurrent with bulk
   batches on the same session from another thread.

It prints a report and the loader and batch-cap decision, and writes the numbers as JSON.
"""

from __future__ import annotations

import argparse
import itertools
import json
import platform
import queue
import random
import statistics
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray
from tokenizers import Tokenizer

REPO = "Snowflake/snowflake-arctic-embed-m-v1.5"
REVISION = "e58a8f756156a1293d763f17e3aae643474e9b8a"
MODEL_FILE = "onnx/model_int8.onnx"
FILES = [
    MODEL_FILE,
    "config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
]
DIM = 768
MAX_LENGTH = 512
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
QUERY_BUDGET_MS = 100.0

Vectors = NDArray[np.float32]


# --- Loaders -----------------------------------------------------------------------------


def fetch_model(model_dir: Path) -> Path:
    """Download the model files once into `model_dir`, standing in for the app bundle."""
    from huggingface_hub import hf_hub_download

    for name in FILES:
        if not (model_dir / name).exists():
            hf_hub_download(REPO, name, revision=REVISION, local_dir=model_dir)
    return model_dir


class DirectEmbedder:
    """The §7.1 fallback: onnxruntime + tokenizers, CLS pooling, L2 normalization."""

    def __init__(self, model_dir: Path, threads: int = 0) -> None:
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=MAX_LENGTH)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(model_dir / MODEL_FILE), options, providers=["CPUExecutionProvider"]
        )

    def token_lengths(self, texts: Sequence[str]) -> list[int]:
        self.tokenizer.no_padding()
        try:
            return [len(e.ids) for e in self.tokenizer.encode_batch(list(texts))]
        finally:
            self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

    def run(self, ids: NDArray[np.int64], mask: NDArray[np.int64]) -> Vectors:
        (tokens,) = self.session.run(
            ["token_embeddings"], {"input_ids": ids, "attention_mask": mask}
        )
        cls = np.asarray(tokens, dtype=np.float32)[:, 0]
        return cls / np.linalg.norm(cls, axis=1, keepdims=True)

    def embed(self, texts: Sequence[str]) -> Vectors:
        encodings = self.tokenizer.encode_batch(list(texts))
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        return self.run(ids, mask)

    def embed_query(self, query: str) -> Vectors:
        return self.embed([QUERY_PREFIX + query])


class FastembedEmbedder:
    """fastembed 0.9.1 has no v1.5 entry, so register the int8 file as a custom model."""

    NAME = "ontological/snowflake-arctic-embed-m-v1.5-int8"

    def __init__(self, model_dir: Path, threads: int = 0) -> None:
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType

        if self.NAME not in {m["model"] for m in TextEmbedding.list_supported_models()}:
            TextEmbedding.add_custom_model(
                model=self.NAME,
                pooling=PoolingType.CLS,
                normalization=True,
                sources=ModelSource(hf=REPO),
                dim=DIM,
                model_file=MODEL_FILE,
                output_name="token_embeddings",
            )
        self.model = TextEmbedding(
            self.NAME,
            specific_model_path=str(model_dir),
            threads=threads or None,
            providers=["CPUExecutionProvider"],
        )

    def embed(self, texts: Sequence[str], batch_size: int = 256) -> Vectors:
        return np.stack(list(self.model.embed(list(texts), batch_size=batch_size)))

    def embed_query(self, query: str) -> Vectors:
        return self.embed([QUERY_PREFIX + query])


# --- Synthetic catalog texts ---------------------------------------------------------------

NOUNS = (  # noqa: SIM905
    "customer account invoice order line item product sku vendor supplier region store "
    "employee payment refund shipment warehouse inventory price discount tax currency "
    "campaign channel session subscription plan contract ledger journal budget forecast "
    "rental film actor category address city country postal phone email status segment"
).split()
TYPES = ["integer", "bigint", "varchar(64)", "text", "numeric(12,2)", "date", "timestamp", "bool"]
VERBS = "records tracks stores links identifies lists totals flags describes".split()  # noqa: SIM905


def _ident(rng: random.Random, parts: int) -> str:
    words = rng.sample(NOUNS, parts)
    if rng.random() < 0.3:
        return words[0] + "".join(w.title() for w in words[1:])
    return "_".join(w[: rng.choice([3, 4, 10])] for w in words)


def _phrase(rng: random.Random, low: int, high: int) -> str:
    return " ".join(rng.choice(NOUNS) for _ in range(rng.randint(low, high)))


def catalog_texts(n: int, seed: int = 7) -> list[str]:
    """Short texts like the catalog's: tables, columns and glossary terms (§7.2)."""
    rng = random.Random(seed)
    texts: list[str] = []
    for _ in range(n):
        kind = rng.random()
        if kind < 0.15:
            cols = ", ".join(_ident(rng, rng.randint(1, 3)) for _ in range(rng.randint(3, 14)))
            texts.append(
                f"Table {_ident(rng, 1)}.{_ident(rng, 2)}: {rng.choice(VERBS)} "
                f"{_phrase(rng, 3, 25)}. Columns: {cols}."
            )
        elif kind < 0.9:
            samples = ", ".join(str(rng.randint(1, 99999)) for _ in range(rng.randint(0, 5)))
            synonyms = ", ".join(_phrase(rng, 1, 2) for _ in range(rng.randint(0, 3)))
            texts.append(
                f"Column {_ident(rng, 2)}.{_ident(rng, rng.randint(1, 3))} ({rng.choice(TYPES)}): "
                f"{rng.choice(VERBS)} {_phrase(rng, 2, 18)}. Synonyms: {synonyms}. "
                f"Sample values: {samples}."
            )
        else:
            texts.append(
                f"Term '{_phrase(rng, 1, 3)}': {_phrase(rng, 5, 40)}. "
                f"Computed from {_ident(rng, 2)} and {_ident(rng, 2)}."
            )
    return texts


QUESTIONS = [
    "top customers by total revenue last quarter",
    "which films were rented most often in 2005",
    "average invoice amount per country",
    "how many orders shipped late from each warehouse",
    "monthly refund rate by payment channel",
    "employees with the most sales this year",
    "inventory value per store and category",
    "subscription churn by plan and segment",
]


# --- Measurements --------------------------------------------------------------------------


def percentiles(samples_ms: Sequence[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)

    def at(p: float) -> float:
        return ordered[min(len(ordered) - 1, round(p * (len(ordered) - 1)))]

    return {
        "n": len(ordered),
        "p50": round(at(0.50), 1),
        "p95": round(at(0.95), 1),
        "max": round(ordered[-1], 1),
        "mean": round(statistics.fmean(ordered), 1),
    }


def timed(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000


def shape_grid(embedder: DirectEmbedder, seq_lens: Sequence[int]) -> list[dict[str, Any]]:
    """Batch time for each batch size x sequence length, stopping a row past 3x budget."""
    rng = np.random.default_rng(0)
    rows: list[dict[str, Any]] = []
    for seq in seq_lens:
        bs = 1
        while bs <= 1024:
            ids = rng.integers(1000, 30000, size=(bs, seq), dtype=np.int64)
            ids[:, 0], ids[:, -1] = 101, 102
            mask = np.ones_like(ids)
            embedder.run(ids, mask)  # warm this shape
            reps = 3 if bs * seq > 8192 else 8
            ms = statistics.median(
                timed(lambda ids=ids, mask=mask: embedder.run(ids, mask)) for _ in range(reps)
            )
            rows.append({"seq": seq, "batch": bs, "tokens": bs * seq, "ms": round(ms, 1)})
            if ms > 3 * QUERY_BUDGET_MS:
                break
            bs *= 2
    return rows


def tokens_per_ms(grid: Sequence[dict[str, Any]]) -> float:
    """The slowest rate over batches of 256+ padded tokens, where the rate has levelled off.

    Batch time is close to linear in padded tokens (batch size x longest sequence), so a
    batch cap is a token budget: budget_ms x this rate.
    """
    return min(r["tokens"] / r["ms"] for r in grid if r["tokens"] >= 256)


def token_batches(lengths: Sequence[int], cap_tokens: int) -> list[list[int]]:
    """Sort by length, then fill each batch while batch size x longest length <= cap."""
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    batches: list[list[int]] = []
    current: list[int] = []
    for i in order:
        if current and (len(current) + 1) * lengths[i] > cap_tokens:
            batches.append(current)
            current = []
        current.append(i)
    if current:
        batches.append(current)
    return batches


class PriorityWorker:
    """One worker thread owns the session. Queries jump the queue; bulk batches fill the gaps.

    The bulk job never runs dry here (it cycles its batches), which is the worst case for
    a query: there is always a batch in flight when it arrives.
    """

    def __init__(self, embedder: DirectEmbedder, batches: list[list[str]]) -> None:
        self.embedder = embedder
        self.batches = itertools.cycle(batches)
        self.queries: queue.SimpleQueue[tuple[str, Future[Vectors]]] = queue.SimpleQueue()
        self.stop = threading.Event()
        self.batch_ms: list[float] = []
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def submit_query(self, text: str) -> Future[Vectors]:
        future: Future[Vectors] = Future()
        self.queries.put((text, future))
        return future

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                text, future = self.queries.get_nowait()
            except queue.Empty:
                batch = next(self.batches)
                self.batch_ms.append(timed(lambda batch=batch: self.embedder.embed(batch)))
            else:
                future.set_result(self.embedder.embed_query(text))


def query_latency_idle(embed_query: Callable[[str], object], n: int) -> dict[str, float]:
    for q in QUESTIONS:
        embed_query(q)
    return percentiles(
        [timed(lambda i=i: embed_query(QUESTIONS[i % len(QUESTIONS)])) for i in range(n)]
    )


def query_latency_priority(
    embedder: DirectEmbedder, batches: list[list[str]], n: int
) -> dict[str, Any]:
    """Bulk batches run back to back on the worker; queries arrive at random times."""
    worker = PriorityWorker(embedder, batches)
    rng = random.Random(1)
    samples: list[float] = []
    for i in range(n):
        time.sleep(rng.uniform(0.02, 0.15))
        start = time.perf_counter()
        worker.submit_query(QUESTIONS[i % len(QUESTIONS)]).result()
        samples.append((time.perf_counter() - start) * 1000)
    worker.stop.set()
    worker.thread.join()
    return {"query": percentiles(samples), "bulk_batch": percentiles(worker.batch_ms)}


def query_latency_concurrent(
    embedder: DirectEmbedder, batches: list[list[str]], n: int
) -> dict[str, Any]:
    """Bulk batches run on one thread while queries call the same session from another."""
    stop = threading.Event()
    batch_ms: list[float] = []

    def bulk() -> None:
        for batch in itertools.cycle(batches):
            if stop.is_set():
                return
            batch_ms.append(timed(lambda batch=batch: embedder.embed(batch)))

    thread = threading.Thread(target=bulk, daemon=True)
    thread.start()
    rng = random.Random(1)
    samples: list[float] = []
    for i in range(n):
        time.sleep(rng.uniform(0.02, 0.15))
        samples.append(timed(lambda i=i: embedder.embed_query(QUESTIONS[i % len(QUESTIONS)])))
    stop.set()
    thread.join()
    return {"query": percentiles(samples), "bulk_batch": percentiles(batch_ms)}


# --- Report --------------------------------------------------------------------------------


def machine() -> dict[str, Any]:
    def sysctl(key: str) -> str:
        try:
            return subprocess.run(
                ["sysctl", "-n", key], capture_output=True, text=True, check=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return "?"

    return {
        "cpu": sysctl("machdep.cpu.brand_string"),
        "cores": sysctl("hw.physicalcpu"),
        "perf_cores": sysctl("hw.perflevel0.physicalcpu"),
        "memory_gb": sysctl("hw.memsize"),
        "macos": platform.mac_ver()[0],
        "onnxruntime": ort.__version__,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--model-dir", type=Path, default=Path.home() / ".cache/ontological/s7-model"
    )
    parser.add_argument("--bulk", type=int, default=100_000, help="texts in the bulk run")
    parser.add_argument("--queries", type=int, default=200, help="queries per latency scenario")
    parser.add_argument("--threads", type=int, default=0, help="intra-op threads; 0 = ORT default")
    parser.add_argument("--out", type=Path, default=None, help="write results JSON here")
    parser.add_argument("--quick", action="store_true", help="5,000 bulk texts, 50 queries")
    args = parser.parse_args()
    if args.quick:
        args.bulk, args.queries = 5_000, 50

    results: dict[str, Any] = {"machine": machine(), "model": f"{REPO}@{REVISION[:7]} {MODEL_FILE}"}
    print(json.dumps(results, indent=2))

    model_dir = fetch_model(args.model_dir)
    results["model_mb"] = round((model_dir / MODEL_FILE).stat().st_size / 1e6, 1)

    # 1. Load both ways, offline.
    load_ms: dict[str, float] = {}
    start = time.perf_counter()
    direct = DirectEmbedder(model_dir, args.threads)
    load_ms["direct"] = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    fast = FastembedEmbedder(model_dir, args.threads)
    load_ms["fastembed"] = (time.perf_counter() - start) * 1000
    results["load_ms"] = {k: round(v) for k, v in load_ms.items()}
    print(f"\nloaded: {results['load_ms']} ms, model file {results['model_mb']} MB")

    # 2. Parity: same vectors from both loaders, for passages and queries.
    sample = catalog_texts(256, seed=99)
    a = np.vstack([direct.embed(sample[i : i + 64]) for i in range(0, len(sample), 64)])
    b = fast.embed(sample, batch_size=64)
    qa = np.vstack([direct.embed_query(q) for q in QUESTIONS])
    qb = np.vstack([fast.embed_query(q) for q in QUESTIONS])
    alone = np.vstack([direct.embed([t]) for t in sample[:64]])
    parity = {
        "passage_min_cosine": float(np.min(np.sum(a * b, axis=1))),
        "query_min_cosine": float(np.min(np.sum(qa * qb, axis=1))),
        # The file's own pooled output, as a check that CLS pooling is what it does.
        "sentence_embedding_min_cosine": float(np.min(np.sum(a * _pooled(direct, sample), axis=1))),
        # The int8 graph quantizes activations per batch (DynamicQuantizeLinear), so a
        # passage's vector depends on its batch-mates, padding included.
        "alone_vs_batched_min_cosine": float(np.min(np.sum(alone * a[:64], axis=1))),
    }
    results["parity"] = parity
    print(f"parity: {parity}")
    assert parity["passage_min_cosine"] > 0.9999 and parity["query_min_cosine"] > 0.9999

    # 3. Shape grid -> token cap.
    grid = shape_grid(direct, [16, 32, 64, 128, 256, 512])
    rate = tokens_per_ms(grid)
    cap = int(QUERY_BUDGET_MS * rate)
    results["shape_grid"] = grid
    results["tokens_per_ms"] = round(rate, 2)
    results["token_cap"] = cap
    print("\nbatch ms by sequence length x batch size:")
    for seq in sorted({r["seq"] for r in grid}):
        cells = "  ".join(f"{r['batch']}:{r['ms']}" for r in grid if r["seq"] == seq)
        print(f"  seq {seq:>3}  {cells}")
    print(
        f"slowest rate {rate:.1f} tokens/ms -> token cap for {QUERY_BUDGET_MS:.0f} ms: "
        f"{cap} padded tokens"
    )

    # 4. Bulk run.
    texts = catalog_texts(args.bulk)
    lengths = direct.token_lengths(texts)
    results["bulk_token_lengths"] = {
        "p50": int(np.percentile(lengths, 50)),
        "p95": int(np.percentile(lengths, 95)),
        "max": max(lengths),
    }
    print(f"\nbulk texts: {args.bulk}, token lengths {results['bulk_token_lengths']}")
    batches = token_batches(lengths, cap)
    text_cap = cap // max(lengths)  # a count cap that holds even for the longest text
    bulk: dict[str, Any] = {"batches_token_capped": len(batches), "text_cap": text_cap}

    def bulk_run(label: str, fn: Callable[[], object]) -> None:
        seconds = timed(fn) / 1000
        bulk[label] = {"seconds": round(seconds, 1), "texts_per_s": round(args.bulk / seconds)}
        print(f"  {label}: {bulk[label]}")

    bulk_run(
        "direct_sorted_token_cap",
        lambda: [direct.embed([texts[i] for i in batch]) for batch in batches],
    )
    bulk_run(
        "direct_unsorted_text_cap",
        lambda: [direct.embed(texts[i : i + text_cap]) for i in range(0, len(texts), text_cap)],
    )
    bulk_run("fastembed_text_cap", lambda: fast.embed(texts, batch_size=text_cap))
    results["bulk"] = bulk

    # 5. Query latency, idle and with bulk batches in flight at the cap and at 4x the cap.
    latency: dict[str, Any] = {
        "idle_direct": query_latency_idle(direct.embed_query, args.queries),
        "idle_fastembed": query_latency_idle(fast.embed_query, args.queries),
    }
    for factor in (1, 4):
        in_flight = [[texts[i] for i in batch] for batch in token_batches(lengths, cap * factor)]
        random.Random(3).shuffle(in_flight)
        latency[f"priority_worker_{factor}x_cap"] = query_latency_priority(
            direct, in_flight, args.queries
        )
        latency[f"concurrent_session_{factor}x_cap"] = query_latency_concurrent(
            direct, in_flight, args.queries
        )
    results["query_latency_ms"] = latency
    print("\nquery latency ms:")
    for label, value in latency.items():
        print(f"  {label}: {value}")

    if args.out:
        args.out.write_text(json.dumps(results, indent=2) + "\n")
        print(f"\nwrote {args.out}")


def _pooled(embedder: DirectEmbedder, texts: Sequence[str]) -> Vectors:
    encodings = embedder.tokenizer.encode_batch(list(texts))
    feed = {
        "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
        "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
    }
    (pooled,) = embedder.session.run(["sentence_embedding"], feed)
    v = np.asarray(pooled, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


if __name__ == "__main__":
    main()
