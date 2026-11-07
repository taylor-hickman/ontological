"""How the embedder runs a candidate's graph (ARCHITECTURE.md §7.2): which inputs it feeds, which
output it reads and how it pools, from the model's spec. A fake graph stands in for ONNX."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pytest

from ontological.embed import embedder as embedder_module
from ontological.embed import model
from ontological.embed.embedder import LocalEmbedder, Tokens, Vectors

# Two texts, three positions: the second text's last position is padding.
IDS = np.array([[101, 7, 102], [101, 102, 0]], dtype=np.int64)
MASK = np.array([[1, 1, 1], [1, 1, 0]], dtype=np.int64)
TOKENS = np.array(
    [
        [[3.0, 0.0], [0.0, 3.0], [3.0, 3.0]],
        [[0.0, 2.0], [2.0, 0.0], [100.0, 100.0]],  # the padding's vector must not count
    ],
    dtype=np.float32,
)


class FakeGraph:
    def __init__(self, output: str) -> None:
        self.output = output
        self.feeds: list[dict[str, Tokens]] = []

    def run(self, output_names: Sequence[str], input_feed: Mapping[str, Tokens]) -> list[Vectors]:
        assert list(output_names) == [self.output]
        self.feeds.append(dict(input_feed))
        return [TOKENS[: len(input_feed["input_ids"])]]


class FakeTokenizer:
    def no_padding(self) -> None: ...

    def enable_truncation(self, max_length: int) -> None: ...


def embedder_for(
    spec: model.ModelSpec, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalEmbedder, FakeGraph]:
    graph = FakeGraph(spec.output)

    def open_session(path: Path, threads: int = 0) -> FakeGraph:
        return graph

    def from_file(path: str) -> FakeTokenizer:
        return FakeTokenizer()

    monkeypatch.setattr(embedder_module, "open_session", open_session)
    monkeypatch.setattr(embedder_module.Tokenizer, "from_file", staticmethod(from_file))
    return LocalEmbedder(Path("/nowhere"), spec=spec), graph


def unit(v: Sequence[float]) -> Vectors:
    a = np.asarray(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def test_a_cls_model_takes_the_first_token(monkeypatch: pytest.MonkeyPatch) -> None:
    e, graph = embedder_for(model.ARCTIC_M, monkeypatch)
    out = e.run(IDS, MASK)
    np.testing.assert_allclose(out, [unit([3, 0]), unit([0, 2])], atol=1e-6)
    assert set(graph.feeds[0]) == {"input_ids", "attention_mask"}
    assert e.model == model.ARCTIC_M.id and e.dim == model.ARCTIC_M.dim


def test_a_mean_model_averages_the_real_tokens_and_gets_token_type_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e, graph = embedder_for(model.NOMIC, monkeypatch)
    out = e.run(IDS, MASK)
    np.testing.assert_allclose(out, [unit([2, 2]), unit([1, 1])], atol=1e-6)
    assert set(graph.feeds[0]) == {"input_ids", "attention_mask", "token_type_ids"}
    assert not graph.feeds[0]["token_type_ids"].any()
    assert e.model == model.NOMIC.id and e.dim == 768


def test_every_candidate_reads_its_own_output(monkeypatch: pytest.MonkeyPatch) -> None:
    for spec in model.CANDIDATES.values():
        e, graph = embedder_for(spec, monkeypatch)
        e.run(IDS, MASK)
        assert ("token_type_ids" in graph.feeds[0]) == spec.token_type_ids
