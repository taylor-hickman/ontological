"""The local embedder behind LangChain's `Embeddings` (ARCHITECTURE.md §5.6, §7.1, spike S7).

It loads a model's ONNX file (`ontological.embed.model`, the default unless a spec is given)
through onnxruntime on the CPU provider and its tokenizer through `tokenizers`, runs the graph,
pools the token vectors as the model card says (the CLS vector, or the mean over the real tokens)
and L2-normalizes the result.

- Bulk texts are sorted by length and batched by padded tokens (batch size x longest text), at
  most `cap_tokens` a batch. Cost is linear in padded tokens, so the cap costs no throughput and
  sorting cuts padding. `calibrate` sets the cap at engine start from one timed batch, so that a
  batch takes about 100 ms, up to 1,024 tokens.
- A query is embedded alone, so its vector is deterministic: the int8 graph quantizes activations
  per batch, so a passage's vector shifts slightly with its batch-mates. Queries call the same
  session as bulk batches, from their own threads, with no queue: onnxruntime releases the GIL
  and takes concurrent `run` calls, and S7 measured a query at about 7 ms with a batch in flight.

Nothing here mutates shared state after `__init__` except `cap_tokens`: the tokenizer never pads
(batches are padded here), so concurrent callers can share it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

import numpy as np
from langchain_core.embeddings import Embeddings
from numpy.typing import NDArray
from tokenizers import Tokenizer

from ontological.embed import model as files

Vectors = NDArray[np.float32]
Tokens = NDArray[np.int64]

CAP_TOKENS = 1024  # the most padded tokens a bulk batch holds, whatever calibration finds
MIN_CAP_TOKENS = 64
TARGET_MS = 100.0  # how long a bulk batch should take
CALIBRATION_SHAPE = (16, 64)  # 1,024 padded tokens, past where the rate levels off (S7)
PAD_ID = 0
CLS_ID, SEP_ID = 101, 102


def token_batches(lengths: Sequence[int], cap_tokens: int) -> list[list[int]]:
    """Indices into `lengths`, shortest first, in batches whose size x longest length stays within
    `cap_tokens`. A text longer than the cap goes alone."""
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


def cap_from_timing(
    tokens: int,
    ms: float,
    *,
    target_ms: float = TARGET_MS,
    ceiling: int = CAP_TOKENS,
    floor: int = MIN_CAP_TOKENS,
) -> int:
    """The padded tokens a batch can hold and still take `target_ms`, at the rate measured."""
    return max(floor, min(ceiling, int(target_ms * tokens / ms)))


class Session(Protocol):
    """The part of onnxruntime's `InferenceSession` the embedder uses (onnxruntime has no stubs)."""

    def run(
        self, output_names: Sequence[str], input_feed: Mapping[str, Tokens]
    ) -> list[Vectors]: ...


def open_session(path: Path, threads: int = 0) -> Session:
    """The graph on the CPU provider; `threads` 0 is onnxruntime's default."""
    # Imported here, not at the top: on macOS, importing onnxruntime opens a guarded descriptor,
    # and the engine closes every inherited descriptor first thing in main(). Closing a guarded
    # one kills the process.
    import onnxruntime as ort  # pyright: ignore[reportMissingTypeStubs]

    options = ort.SessionOptions()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    options.intra_op_num_threads = threads
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL  # pyright: ignore[reportUnknownMemberType]
    session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])  # pyright: ignore[reportUnknownArgumentType]
    return cast(Session, session)


class LocalEmbedder(Embeddings):
    def __init__(
        self,
        model_dir: Path,
        *,
        spec: files.ModelSpec = files.DEFAULT,
        cap_tokens: int = CAP_TOKENS,
        threads: int = 0,
    ) -> None:
        self.spec = spec
        self.model: str = spec.id
        self.dim: int = spec.dim
        self.cap_tokens = cap_tokens
        self.tokenizer = Tokenizer.from_file(str(model_dir / spec.tokenizer_file))
        self.tokenizer.no_padding()
        self.tokenizer.enable_truncation(max_length=spec.max_length)  # pyright: ignore[reportUnknownMemberType]
        self.session = open_session(model_dir / spec.model_file, threads)

    # --- the graph -----------------------------------------------------------------------------

    def run(self, ids: Tokens, mask: Tokens) -> Vectors:
        """One batch through the graph: pooled vectors, L2-normalized."""
        feed = {"input_ids": ids, "attention_mask": mask}
        if self.spec.token_type_ids:
            feed["token_type_ids"] = np.zeros_like(ids)
        (tokens,) = self.session.run([self.spec.output], feed)
        tokens = np.asarray(tokens, dtype=np.float32)
        if self.spec.pooling == "cls":
            pooled = tokens[:, 0]
        else:
            weights = mask[:, :, None].astype(np.float32)
            pooled = (tokens * weights).sum(axis=1) / weights.sum(axis=1)
        return pooled / np.linalg.norm(pooled, axis=1, keepdims=True)

    def _encode(self, texts: Sequence[str]) -> list[list[int]]:
        return [e.ids for e in self.tokenizer.encode_batch(list(texts))]

    @staticmethod
    def _pad(encoded: Sequence[Sequence[int]]) -> tuple[Tokens, Tokens]:
        width = max(len(e) for e in encoded)
        ids = np.full((len(encoded), width), PAD_ID, dtype=np.int64)
        mask = np.zeros((len(encoded), width), dtype=np.int64)
        for row, e in enumerate(encoded):
            ids[row, : len(e)] = e
            mask[row, : len(e)] = 1
        return ids, mask

    # --- bulk ----------------------------------------------------------------------------------

    def embed_batches(self, texts: Sequence[str]) -> Iterator[tuple[list[int], Vectors]]:
        """Embed `texts` as passages, one capped batch at a time: yields each batch's indices into
        `texts` and its vectors, so a job can report progress and stop between batches."""
        encoded = self._encode([self.spec.passage_prefix + t for t in texts])
        for batch in token_batches([len(e) for e in encoded], self.cap_tokens):
            yield batch, self.run(*self._pad([encoded[i] for i in batch]))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out = np.empty((len(texts), self.dim), dtype=np.float32)
        for batch, vectors in self.embed_batches(texts):
            out[batch] = vectors
        return out.tolist()

    # --- queries -------------------------------------------------------------------------------

    def embed_query(self, text: str) -> list[float]:
        """One query, alone in its batch, so its vector doesn't depend on what else is running."""
        return self.run(*self._pad(self._encode([self.spec.query_prefix + text])))[0].tolist()

    # --- calibration ---------------------------------------------------------------------------

    def calibrate(
        self, *, target_ms: float = TARGET_MS, clock: Callable[[], float] = time.perf_counter
    ) -> int:
        """Time one batch and set `cap_tokens` to what `target_ms` holds at that rate (up to
        1,024). A first, untimed run warms the session for the shape."""
        rows, width = CALIBRATION_SHAPE
        ids = np.random.default_rng(0).integers(1000, 30000, size=(rows, width), dtype=np.int64)
        ids[:, 0], ids[:, -1] = CLS_ID, SEP_ID
        mask = np.ones_like(ids)
        self.run(ids, mask)
        start = clock()
        self.run(ids, mask)
        ms = (clock() - start) * 1000
        self.cap_tokens = cap_from_timing(rows * width, ms, target_ms=target_ms)
        return self.cap_tokens
