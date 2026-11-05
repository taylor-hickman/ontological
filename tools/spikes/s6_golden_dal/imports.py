"""List the upstream modules that ARCHITECTURE.md §11's vendored files need from outside it.

    python -I imports.py <upstream checkout> [<loaded.txt>]

Needs Python 3.10 or later (`sys.stdlib_module_names`). Imports are read with `ast`, so
nothing upstream runs.

The vendored set is the engine's (`dal/`, `catalog/store/`, `vdb/postgres.py`, `alembic/`) plus
§11's test-only files: the golden seed and capture, upstream's Postgres and SQLite connectors,
the two service modules, and the DAL tests. Each import of an `auto_ontology` or `dev_tools`
module outside that set is either an edit to the vendored copy or another file to vendor.
Imports at module level must resolve for the file to load at all; imports inside a function
(marked "lazy") only matter on the paths that call them.

<loaded.txt> is what s6_loaded.py wrote during run.sh: every upstream module the seed and the
DAL tests actually loaded. The ones outside the set are what the golden suite exercises beyond
§11's list.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

# Globs relative to the checkout, from ARCHITECTURE.md §11.
ENGINE = (
    "auto_ontology/dal/*.py",
    "auto_ontology/catalog/store/*.py",
    "auto_ontology/vdb/postgres.py",
    "alembic/env.py",
    "alembic/versions/*.py",
)
TEST_ONLY = (
    "dev_tools/fixtures/seed_graph_fixture.py",
    "dev_tools/fixtures/capture_dal_golden.py",
    "auto_ontology/connectors/postgres.py",
    "auto_ontology/connectors/sqlite.py",
    "auto_ontology/server/sql_attributes/service.py",
    "auto_ontology/server/custom_analyses/service.py",
    "auto_ontology/dal/tests/*.py",
)
# Package __init__ files come along with the modules under them.
ENGINE_PACKAGES = (
    "auto_ontology/__init__.py",
    "auto_ontology/dal/__init__.py",
    "auto_ontology/catalog/__init__.py",
    "auto_ontology/catalog/store/__init__.py",
    "auto_ontology/vdb/__init__.py",
)
TEST_PACKAGES = (
    "auto_ontology/connectors/__init__.py",
    "auto_ontology/server/__init__.py",
    "auto_ontology/server/sql_attributes/__init__.py",
    "auto_ontology/server/custom_analyses/__init__.py",
    "dev_tools/__init__.py",
    "dev_tools/fixtures/__init__.py",
)
PACKAGES = ENGINE_PACKAGES + TEST_PACKAGES
ROOTS = ("auto_ontology", "dev_tools")
TYPE_CHECKING = ("TYPE_CHECKING", "typing.TYPE_CHECKING")


def module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def module_path(root: Path, mod: str) -> Path:
    base = root.joinpath(*mod.split("."))
    return base.with_suffix(".py") if base.with_suffix(".py").is_file() else base / "__init__.py"


def resolve(root: Path, name: str) -> str | None:
    """The module file *name* refers to, as a dotted name, or None if it is not upstream's."""
    parts = name.split(".")
    while parts:
        if exists(root, [*parts[:-1], parts[-1] + ".py"]) or exists(root, [*parts, "__init__.py"]):
            return ".".join(parts)
        parts.pop()  # `from pkg.mod import name`: name is an attribute, not a module
    return None


def exists(root: Path, parts: list[str]) -> bool:
    """Whether root/parts exists with exactly this case (macOS file systems ignore case, so
    `from catalog.model import Schema` would otherwise resolve to `model/schema.py`)."""
    path = root
    for part in parts:
        if not path.is_dir() or part not in {p.name for p in path.iterdir()}:
            return False
        path = path / part
    return True


def parse_imports(path: Path, own: str) -> list[tuple[str, bool]]:
    """(imported name, lazy) for every import in *path*; lazy means inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = own if path.name == "__init__.py" else own.rpartition(".")[0]
    found: list[tuple[str, bool]] = []

    def visit(node: ast.AST, lazy: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            lazy = True
        if isinstance(node, ast.If) and ast.unparse(node.test) in TYPE_CHECKING:
            for child in node.orelse:  # the `if TYPE_CHECKING:` body never runs
                visit(child, lazy)
            return
        if isinstance(node, ast.Import):
            found.extend((a.name, lazy) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join(anchor + ([base] if base else []))
            found.append((base, lazy))
            found.extend((f"{base}.{a.name}", lazy) for a in node.names)
        for child in ast.iter_child_nodes(node):
            visit(child, lazy)

    visit(tree, False)
    return found


def upstream_imports(root: Path, mod: str) -> dict[str, bool]:
    """Upstream modules *mod* imports, each mapped to whether every import of it is lazy."""
    out: dict[str, bool] = {}
    for name, lazy in parse_imports(module_path(root, mod), mod):
        if name.split(".")[0] not in ROOTS:
            continue
        target = resolve(root, name)
        if not target:
            continue
        # Importing a.b.c runs a/__init__ and a/b/__init__ first.
        parts = target.split(".")
        for i in range(1, len(parts) + 1):
            dotted = ".".join(parts[:i])
            if dotted != mod and not mod.startswith(dotted + "."):
                out[dotted] = out.get(dotted, True) and lazy
    return out


def third_party(path: Path) -> set[str]:
    names = {n.split(".")[0] for n, _ in parse_imports(path, "")}
    return {
        n
        for n in names
        if n and n not in sys.stdlib_module_names and n not in ROOTS and n != "__future__"
    }


def closure(root: Path, start: set[str], vendored: set[str], *, eager_only: bool) -> set[str]:
    seen: set[str] = set()
    queue = sorted(start)
    while queue:
        mod = queue.pop()
        if mod in seen or mod in vendored:
            continue
        seen.add(mod)
        for target, lazy in upstream_imports(root, mod).items():
            if not (eager_only and lazy) and target not in seen and target not in vendored:
                queue.append(target)
    return seen


def closures(
    root: Path, outside: dict[str, dict[str, bool]], vendored: set[str]
) -> tuple[set[str], set[str]]:
    """(needed to import, everything reachable) from the set's outside imports."""
    direct = {m for found in outside.values() for m in found}
    eager = {m for found in outside.values() for m, lazy in found.items() if not lazy}
    return (
        closure(root, eager, vendored, eager_only=True),
        closure(root, direct, vendored, eager_only=False),
    )


def by_package(mods: set[str]) -> list[str]:
    groups: dict[str, list[str]] = defaultdict(list)
    for mod in sorted(mods):
        groups[".".join(mod.split(".")[:2])].append(mod)
    return [
        f"- `{pkg}` ({len(ms)}): "
        + ", ".join(f"`{m.removeprefix(pkg + '.') if m != pkg else '(itself)'}`" for m in ms)
        for pkg, ms in sorted(groups.items())
    ]


def main(checkout: str, loaded_file: str | None) -> int:
    root = Path(checkout).resolve()
    files = {p.relative_to(root) for g in (*ENGINE, *TEST_ONLY, *PACKAGES) for p in root.glob(g)}
    vendored = {module_name(p) for p in files}

    outside: dict[str, dict[str, bool]] = {}
    for rel in sorted(files):
        found = {
            m: lazy
            for m, lazy in upstream_imports(root, module_name(rel)).items()
            if m not in vendored
        }
        if found:
            outside[str(rel)] = found

    print(
        f"## Imports from outside the vendored set ({len(files)} files, "
        f"{len(outside)} with outside imports)"
    )
    print()
    print("| Vendored file | At module level | Lazy |")
    print("|---|---|---|")
    for rel, found in outside.items():
        eager = ", ".join(f"`{m}`" for m in sorted(found) if not found[m]) or "-"
        lazy = ", ".join(f"`{m}`" for m in sorted(found) if found[m]) or "-"
        print(f"| `{rel}` | {eager} | {lazy} |")

    to_import, everything = closures(root, outside, vendored)
    print()
    print(f"## Needed just to import the set: {len(to_import)} more modules")
    print()
    print("\n".join(by_package(to_import)))
    print()
    print(f"## Everything reachable, lazy imports included: {len(everything)} more modules")
    print()
    print("\n".join(by_package(everything)))

    engine_files = {p.relative_to(root) for g in (*ENGINE, *ENGINE_PACKAGES) for p in root.glob(g)}
    engine = {module_name(p) for p in engine_files}
    engine_outside = {
        str(rel): {
            m: lazy
            for m, lazy in upstream_imports(root, module_name(rel)).items()
            if m not in engine
        }
        for rel in engine_files
    }
    engine_import, _ = closures(root, engine_outside, engine)
    print()
    print(
        f"## Needed to import the engine's part alone ({len(engine_files)} files): "
        f"{len(engine_import)} more modules"
    )
    print()
    print("\n".join(by_package(engine_import)))

    if loaded_file:
        loaded = set(Path(loaded_file).read_text(encoding="utf-8").split())
        ran = {m for m in loaded if m not in vendored and resolve(root, m) == m}
        print()
        print(f"## Loaded by the seed and the DAL tests: {len(ran)} modules outside the set")
        print()
        print("\n".join(by_package(ran)))

    print()
    print("## Third-party packages imported (stdlib left out)")
    print()
    for label, mods in (
        ("vendored set", vendored),
        ("needed to import", to_import),
        ("everything reachable", everything),
    ):
        found: set[str] = set()
        for mod in mods:
            if module_path(root, mod).is_file():
                found |= third_party(module_path(root, mod))
        print(f"- {label}: " + ", ".join(f"`{p}`" for p in sorted(found)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
