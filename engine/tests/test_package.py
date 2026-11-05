import sys
import tomllib
from pathlib import Path

import ontological

ENGINE_DIR = Path(__file__).resolve().parents[1]


def test_runs_on_python_312() -> None:
    assert sys.version_info[:2] == (3, 12)


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((ENGINE_DIR / "pyproject.toml").read_text())
    assert ontological.__version__ == pyproject["project"]["version"]


def test_package_is_typed() -> None:
    assert (Path(ontological.__file__).parent / "py.typed").is_file()
