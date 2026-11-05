"""The local embedder on the real model (ARCHITECTURE.md §5.6, §7.1). Skips without the model."""

from __future__ import annotations

import threading

import numpy as np
import pytest
from langchain_core.embeddings import Embeddings

from ontological.embed import model
from ontological.embed.embedder import CAP_TOKENS, LocalEmbedder, Tokens, Vectors

TEXTS = [
    "Column invoice.total (numeric(10,2)): the amount billed on the invoice.",
    "Table customer: one row per customer, with name, email and country.",
    "Column track.milliseconds (integer): the length of a track.",
    "Term 'revenue': the sum of invoice line unit price times quantity.",
    "Column employee.hire_date (timestamp): when the employee started.",
]


def cosine(a: list[float], b: list[float]) -> float:
    return float(np.dot(a, b))


def test_it_is_a_langchain_embeddings_with_unit_vectors_of_the_models_dimension(
    embedder: LocalEmbedder,
) -> None:
    assert isinstance(embedder, Embeddings)
    assert embedder.dim == model.DIM == 768
    assert embedder.model == model.MODEL_ID
    vectors = embedder.embed_documents(TEXTS)
    query = embedder.embed_query("total amount billed")
    for v in [*vectors, query]:
        assert len(v) == model.DIM
        assert np.linalg.norm(v) == pytest.approx(1.0, abs=1e-5)


def test_documents_come_back_in_input_order_whatever_the_batching(
    embedder: LocalEmbedder,
) -> None:
    together = embedder.embed_documents(TEXTS)
    alone = [embedder.embed_documents([t])[0] for t in TEXTS]
    # int8 activations are quantized per batch, so batch-mates shift a vector slightly (S7).
    for a, b in zip(together, alone, strict=True):
        assert cosine(a, b) >= 0.99


def test_queries_are_deterministic_and_carry_the_query_prefix(embedder: LocalEmbedder) -> None:
    q = "which customers spent the most"
    assert embedder.embed_query(q) == embedder.embed_query(q)
    assert embedder.embed_query(q) != embedder.embed_documents([q])[0]
    assert embedder.embed_query(q) == embedder.embed_documents([model.QUERY_PREFIX + q])[0]


def test_a_query_finds_its_passage(embedder: LocalEmbedder) -> None:
    passages = embedder.embed_documents(TEXTS)
    query = embedder.embed_query("when did each employee join the company")
    best = max(range(len(TEXTS)), key=lambda i: cosine(query, passages[i]))
    assert TEXTS[best].startswith("Column employee.hire_date")


def test_bulk_batches_stay_within_the_cap(
    embedder: LocalEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    texts = [" ".join(["word"] * n) for n in range(1, 400, 7)]
    shapes: list[tuple[int, ...]] = []
    original = embedder.run

    def spy(ids: Tokens, mask: Tokens) -> Vectors:
        shapes.append(ids.shape)
        return original(ids, mask)

    monkeypatch.setattr(embedder, "cap_tokens", 256)
    monkeypatch.setattr(embedder, "run", spy)
    vectors = embedder.embed_documents(texts)

    assert len(vectors) == len(texts)
    assert len(shapes) > 1
    assert all(rows * width <= 256 or rows == 1 for rows, width in shapes)


def test_calibration_sets_the_cap_from_one_timed_batch(
    embedder: LocalEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter([10.0, 10.2])  # the timed batch took 200 ms
    monkeypatch.setattr(embedder, "cap_tokens", embedder.cap_tokens)  # restored afterwards
    assert embedder.calibrate(clock=lambda: next(times)) == 512
    assert embedder.cap_tokens == 512


def test_calibration_on_this_machine_stays_within_the_ceiling(
    embedder: LocalEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embedder, "cap_tokens", embedder.cap_tokens)
    assert 64 <= embedder.calibrate() <= CAP_TOKENS == 1024


def test_a_query_does_not_wait_for_a_bulk_batch_in_flight(
    embedder: LocalEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No queue: the query calls the same session while the batch runs (§5.6)."""
    running = threading.Event()
    bulk_done = threading.Event()
    original = embedder.run

    def spy(ids: Tokens, mask: Tokens) -> Vectors:
        if ids.shape[0] > 1:
            running.set()
        return original(ids, mask)

    # One batch of 64 texts x 512 tokens: 32x the cap, seconds of work, so the query lands inside
    # it. Behind a queue the query would wait for all of it.
    monkeypatch.setattr(embedder, "cap_tokens", 64 * 512)
    monkeypatch.setattr(embedder, "run", spy)
    long_texts = [" ".join(["revenue"] * 600)] * 64

    def bulk() -> None:
        embedder.embed_documents(long_texts)
        bulk_done.set()

    thread = threading.Thread(target=bulk)
    thread.start()
    try:
        assert running.wait(30)
        embedder.embed_query("top customers by revenue")
        assert not bulk_done.is_set(), "the query waited for the bulk batch"
    finally:
        thread.join()
