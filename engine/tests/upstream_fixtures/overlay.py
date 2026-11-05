"""Put the test-only upstream modules here under the engine's `auto_ontology` package.

The tree beside this file mirrors upstream's layout. Directories that are packages in the engine
(`auto_ontology/`, `catalog/`, `server/`, `utils/`) have no `__init__.py` here: `install` adds
them to the engine package's `__path__`, so `auto_ontology.catalog.ingest` loads from here while
`auto_ontology.catalog.store` still loads from the engine. Test-only packages (`connectors/`,
`retrieval/`, ...) have their own `__init__.py` and load through their parent.

Only this tree's conftest.py and seed_graph.py call it: the engine never sees these modules.
"""

from __future__ import annotations

import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "auto_ontology"


def install() -> None:
    for directory in sorted(d for d in (ROOT, *ROOT.rglob("*")) if d.is_dir()):
        parts = directory.relative_to(ROOT).parts
        if "tests" in parts or "__pycache__" in parts or (directory / "__init__.py").exists():
            continue
        # Imported even when it holds only tests (dal/, catalog/store/): pytest's importlib mode
        # names a test module auto_ontology.dal.tests.test_x and, for a parent not yet imported,
        # registers an empty stand-in that hides the engine's package.
        package = importlib.import_module(".".join(("auto_ontology", *parts)))
        holds_modules = any(directory.glob("*.py")) or any(directory.glob("*/__init__.py"))
        if holds_modules and str(directory) not in package.__path__:
            package.__path__.append(str(directory))
