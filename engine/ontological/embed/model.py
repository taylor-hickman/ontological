"""The embedding model's files, pinned by revision and SHA-256 (ARCHITECTURE.md §7.1, spike S7).

    python -m ontological.embed.model <dir>

downloads them into <dir> once; the engine loads them from there with no network, as it will
from the app bundle. Only the int8 graph and the tokenizer are needed.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import urllib.request
from collections.abc import Sequence
from pathlib import Path

REPO = "Snowflake/snowflake-arctic-embed-m-v1.5"
REVISION = "e58a8f756156a1293d763f17e3aae643474e9b8a"
MODEL_FILE = "onnx/model_int8.onnx"
TOKENIZER_FILE = "tokenizer.json"
FILES = {
    MODEL_FILE: "a18f437b2466863901a0bdc14904cf93246f5ecce0b656fc773bc2b7b2f84f6e",
    TOKENIZER_FILE: "91f1def9b9391fdabe028cd3f3fcc4efd34e5d1f08c3bf2de513ebb5911a1854",
}
BASE_URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}"

# What every vector's metadata names as its model (§7.1): vectors from different models, or from
# another file of the same one, are never compared.
MODEL_ID = f"{REPO}@{REVISION[:7]}/{MODEL_FILE}"
DIM = 768
MAX_LENGTH = 512
# The model card's prefixes: queries get one, passages none.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
PASSAGE_PREFIX = ""


class ModelError(Exception):
    """A model file is missing or isn't the pinned one."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def verify(model_dir: Path) -> None:
    """Raise ModelError unless every file is in `model_dir` with its pinned checksum."""
    for name, expected in FILES.items():
        path = model_dir / name
        if not path.is_file():
            raise ModelError(f"{path} is missing")
        if _sha256(path) != expected:
            raise ModelError(f"{path} doesn't match its pinned SHA-256")


def fetch(model_dir: Path, *, base_url: str = BASE_URL) -> None:
    """Download each file that isn't already in `model_dir` and verified. A file lands under its
    name only once its checksum matches, so an interrupted or wrong download leaves nothing."""
    for name, expected in FILES.items():
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
    if len(args) != 1:
        print("usage: python -m ontological.embed.model <dir>", file=sys.stderr)
        return 2
    model_dir = Path(args[0])
    fetch(model_dir)
    verify(model_dir)
    print(f"{MODEL_ID} in {model_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
