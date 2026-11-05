"""Record which upstream modules a run loads, so imports.py can compare them with the vendored set.

As a pytest plugin:   S6_LOADED=<file> pytest -p s6_loaded ...
As a wrapper:         S6_LOADED=<file> python -m s6_loaded <module> [args...]

At exit, appends every loaded `auto_ontology` and `dev_tools` module name to $S6_LOADED.
"""

from __future__ import annotations

import atexit
import os
import runpy
import sys

ROOTS = ("auto_ontology", "dev_tools")


def _dump() -> None:
    out = os.environ.get("S6_LOADED")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        for name in sorted(sys.modules):
            if name.split(".")[0] in ROOTS:
                f.write(name + "\n")


atexit.register(_dump)

if __name__ == "__main__":
    module, *args = sys.argv[1:]
    sys.argv = [module, *args]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
