"""Fetching the pinned model files, checked against their SHA-256 (ARCHITECTURE.md §7.1)."""

from __future__ import annotations

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
