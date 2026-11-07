"""The embedding models' files, pinned by revision and SHA-256 (ARCHITECTURE.md §7.1, §7.2,
spike S7).

    python -m ontological.embed.model <dir> [<model>]

downloads one model's files into <dir> once (the default model unless <model> names a candidate);
the engine loads them from there with no network, as it will from the app bundle. Only the ONNX
graph and the tokenizer are needed.

`CANDIDATES` holds §7.2's four candidates, which the retrieval benchmark (§7.4) compares;
`DEFAULT` is the one the engine runs, the benchmark's winner (BUILD_PLAN.md, M3.9). The
module-level names (`MODEL_ID`, `DIM`, `FILES`, ...) are the default's.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ModelSpec:
    """One embedding model: where its files come from, and how to run its graph."""

    key: str  # the short name the benchmark and the CLI use
    repo: str  # on Hugging Face
    revision: str
    model_file: str
    tokenizer_file: str
    files: Mapping[str, str]  # path in the repo -> SHA-256
    dim: int
    output: str  # the graph output holding the token vectors
    pooling: Literal["cls", "mean"]
    query_prefix: str  # the model card's prefixes
    passage_prefix: str
    token_type_ids: bool  # whether the graph takes them (all zeros, one segment)
    # The cosine distance at or under which an attribute hit covers an entity in
    # `check_answerable` (§7.3), calibrated by the retrieval benchmark (M3.9): each model spreads
    # its distances differently, so upstream's 0.75 doesn't transfer.
    coverage_distance: float
    max_length: int = 512

    @property
    def id(self) -> str:
        """What every vector's metadata names as its model (§7.1): vectors from different models,
        or from another file of the same one, are never compared."""
        return f"{self.repo}@{self.revision[:7]}/{self.model_file}"

    @property
    def base_url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/{self.revision}"


_BGE_QUERY = "Represent this sentence for searching relevant passages: "
# bge-small, nomic and bge-base share one tokenizer.json, byte for byte.
_BERT_TOKENIZER = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"

BGE_SMALL = ModelSpec(
    key="bge-small",
    repo="Qdrant/bge-small-en-v1.5-onnx-Q",
    revision="aa8f8b060edb00e03bfdd08813a2949946c8ba55",
    model_file="model_optimized.onnx",
    tokenizer_file="tokenizer.json",
    files={
        "model_optimized.onnx": "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431",
        "tokenizer.json": _BERT_TOKENIZER,
    },
    dim=384,
    output="last_hidden_state",
    pooling="cls",
    query_prefix=_BGE_QUERY,
    passage_prefix="",
    token_type_ids=True,
    coverage_distance=0.346,
)

ARCTIC_M = ModelSpec(
    key="arctic-m",
    repo="Snowflake/snowflake-arctic-embed-m-v1.5",
    revision="e58a8f756156a1293d763f17e3aae643474e9b8a",
    model_file="onnx/model_int8.onnx",
    tokenizer_file="tokenizer.json",
    files={
        "onnx/model_int8.onnx": "a18f437b2466863901a0bdc14904cf93246f5ecce0b656fc773bc2b7b2f84f6e",
        "tokenizer.json": "91f1def9b9391fdabe028cd3f3fcc4efd34e5d1f08c3bf2de513ebb5911a1854",
    },
    dim=768,
    output="token_embeddings",
    pooling="cls",
    query_prefix=_BGE_QUERY,
    passage_prefix="",
    token_type_ids=False,
    coverage_distance=0.636,
)

NOMIC = ModelSpec(
    key="nomic",
    repo="nomic-ai/nomic-embed-text-v1.5",
    revision="e9b6763023c676ca8431644204f50c2b100d9aab",
    model_file="onnx/model_int8.onnx",
    tokenizer_file="tokenizer.json",
    files={
        "onnx/model_int8.onnx": "b4342336debaea79de872370664b0aaeb67dea4605513d00ee236ea871a81f27",
        "tokenizer.json": _BERT_TOKENIZER,
    },
    dim=768,
    output="last_hidden_state",
    pooling="mean",
    query_prefix="search_query: ",
    passage_prefix="search_document: ",
    token_type_ids=True,
    coverage_distance=0.356,
)

BGE_BASE = ModelSpec(
    key="bge-base",
    repo="BAAI/bge-base-en-v1.5",
    revision="a5beb1e3e68b9ab74eb54cfd186867f64f240e1a",
    model_file="onnx/model.onnx",
    tokenizer_file="tokenizer.json",
    files={
        "onnx/model.onnx": "9bc579acdba21c253c62a9bf866891355a63ffa3442b52c8a37d75b2ccb91848",
        "tokenizer.json": _BERT_TOKENIZER,
    },
    dim=768,
    output="last_hidden_state",
    pooling="cls",
    query_prefix=_BGE_QUERY,
    passage_prefix="",
    token_type_ids=True,
    coverage_distance=0.415,
)

CANDIDATES: Mapping[str, ModelSpec] = {m.key: m for m in (BGE_SMALL, ARCTIC_M, NOMIC, BGE_BASE)}
DEFAULT = ARCTIC_M

REPO = DEFAULT.repo
REVISION = DEFAULT.revision
MODEL_FILE = DEFAULT.model_file
TOKENIZER_FILE = DEFAULT.tokenizer_file
FILES = DEFAULT.files
BASE_URL = DEFAULT.base_url
MODEL_ID = DEFAULT.id
DIM = DEFAULT.dim
MAX_LENGTH = DEFAULT.max_length
QUERY_PREFIX = DEFAULT.query_prefix
PASSAGE_PREFIX = DEFAULT.passage_prefix
COVERAGE_DISTANCE = DEFAULT.coverage_distance


class ModelError(Exception):
    """A model file is missing or isn't the pinned one."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def verify(model_dir: Path, spec: ModelSpec = DEFAULT) -> None:
    """Raise ModelError unless every file is in `model_dir` with its pinned checksum."""
    for name, expected in spec.files.items():
        path = model_dir / name
        if not path.is_file():
            raise ModelError(f"{path} is missing")
        if _sha256(path) != expected:
            raise ModelError(f"{path} doesn't match its pinned SHA-256")


def fetch(model_dir: Path, spec: ModelSpec = DEFAULT, *, base_url: str | None = None) -> None:
    """Download each file that isn't already in `model_dir` and verified. A file lands under its
    name only once its checksum matches, so an interrupted or wrong download leaves nothing."""
    base_url = base_url or spec.base_url
    for name, expected in spec.files.items():
        path = model_dir / name
        if path.is_file() and _sha256(path) == expected:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
            part = Path(tmp.name)
            try:
                with urllib.request.urlopen(f"{base_url}/{name}") as response:
                    shutil.copyfileobj(response, tmp)
            except BaseException:
                part.unlink()
                raise
        if _sha256(part) != expected:
            part.unlink()
            raise ModelError(f"{base_url}/{name} doesn't match its pinned SHA-256")
        part.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (1, 2) or (len(args) == 2 and args[1] not in CANDIDATES):
        print(
            f"usage: python -m ontological.embed.model <dir> [{'|'.join(CANDIDATES)}]",
            file=sys.stderr,
        )
        return 2
    model_dir = Path(args[0])
    spec = CANDIDATES[args[1]] if len(args) == 2 else DEFAULT
    fetch(model_dir, spec)
    verify(model_dir, spec)
    print(f"{spec.id} in {model_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
