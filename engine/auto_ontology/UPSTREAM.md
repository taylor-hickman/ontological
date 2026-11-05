# Vendored upstream: `auto_ontology`

This package is vendored from [NVIDIA/auto-ontology](https://github.com/NVIDIA/auto-ontology) at `2a24f5d34ef6b6e168a7f22d308504d3ab36a198`, under the Apache License 2.0 (`LICENSE`, here). It keeps upstream's package name, `auto_ontology`, as a second top-level package in the engine's distribution, so the files keep their absolute `auto_ontology.*` imports and each one diffs cleanly against upstream (ARCHITECTURE.md §10, §11).

Paths map one to one, except for the Alembic chain. Upstream keeps it at the repository root, and here it is inside the package, so that it ships in the wheel:

| Upstream | Here |
|---|---|
| `alembic/` | `auto_ontology/alembic/` |
| `auto_ontology/<path>` | `auto_ontology/<path>` |

Upstream's `alembic.ini` isn't vendored. `ontological/store/chains.py` configures the chain in code.

## Files

| File | State |
|---|---|
| `alembic/README`, `alembic/script.py.mako`, `alembic/versions/*.py` (7 revisions, head `99d4d343dd0b`) | Unchanged |
| `alembic/env.py` | Modified: drops the `auto_ontology.env` import (python-dotenv) and uses `sqlalchemy.url` when the engine has set it |
| `__init__.py`, `dal/__init__.py`, `dal/schema.py`, `dal/session.py`, `infra/__init__.py` | Unchanged. `env.py` imports `dal.schema` and `dal.session`, which imports `infra.postgres` |
| `dal/schema_version.py` | Modified: finds the chain at `auto_ontology/alembic`, not two levels above `dal/` |
| `infra/postgres.py` | Modified: a `POSTGRES_HOST` that is a socket directory builds `postgresql://user@/db?host=<dir>`, with no password |

Each modified file has a "Modified by Ontological" line under its SPDX header. The rest of upstream's DAL, `catalog/store/` and `vdb/postgres.py` are vendored in M1.1 (docs/BUILD_PLAN.md).

To check a file against upstream: `diff <upstream checkout>/alembic/env.py engine/auto_ontology/alembic/env.py`.
