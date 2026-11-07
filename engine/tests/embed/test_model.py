"""Fetching the pinned model files, checked against their SHA-256 (ARCHITECTURE.md §7.1)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from ontological.embed import model


def test_fetch_copies_the_pinned_files_and_verifies_them(model_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "model"
    model.fetch(out, base_url=model_dir.as_uri())
    model.verify(out)
    assert sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()) == sorted(
        model.FILES
    )


def test_fetch_keeps_files_that_already_verify(model_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "model"
    model.fetch(out, base_url=model_dir.as_uri())
    model.fetch(out, base_url=(tmp_path / "nowhere").as_uri())  # nothing left to download


def test_a_file_with_the_wrong_checksum_is_refused_and_not_kept(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for name in model.FILES:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text("not the model")
    out = tmp_path / "model"
    with pytest.raises(model.ModelError, match="SHA-256"):
        model.fetch(out, base_url=source.as_uri())
    assert not [p for p in out.rglob("*") if p.is_file()]


def test_verify_names_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(model.ModelError, match=r"model_int8\.onnx is missing"):
        model.verify(tmp_path)


def test_the_candidates_are_distinct_and_the_default_is_one_of_them() -> None:
    assert set(model.CANDIDATES) == {"bge-small", "arctic-m", "nomic", "bge-base"}
    assert model.DEFAULT in model.CANDIDATES.values()
    assert len({m.id for m in model.CANDIDATES.values()}) == len(model.CANDIDATES)
    assert model.DEFAULT.id == model.MODEL_ID and model.DEFAULT.dim == model.DIM
    for m in model.CANDIDATES.values():
        assert m.model_file in m.files and m.tokenizer_file in m.files
        assert all(len(sha) == 64 for sha in m.files.values())
        assert 0.0 < m.coverage_distance < 1.0


def test_fetch_and_verify_take_any_candidate(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "graph.onnx").parent.mkdir(parents=True)
    (source / "graph.onnx").write_bytes(b"graph")
    (source / "tokenizer.json").write_bytes(b"{}")
    spec = replace(
        model.BGE_SMALL,
        model_file="graph.onnx",
        files={
            "graph.onnx": hashlib.sha256(b"graph").hexdigest(),
            "tokenizer.json": hashlib.sha256(b"{}").hexdigest(),
        },
    )
    out = tmp_path / "model"
    model.fetch(out, spec, base_url=source.as_uri())
    model.verify(out, spec)
    with pytest.raises(model.ModelError, match="missing"):
        model.verify(out)  # the default's files aren't there


def test_the_cli_refuses_an_unknown_model(tmp_path: Path) -> None:
    assert model.main([str(tmp_path), "nemotron"]) == 2
