# Vendored upstream: `auto_ontology`

This package is vendored from [NVIDIA/auto-ontology](https://github.com/NVIDIA/auto-ontology) at `2a24f5d34ef6b6e168a7f22d308504d3ab36a198`, under the Apache License 2.0 (`LICENSE`, here). It keeps upstream's package name, `auto_ontology`, as a second top-level package in the engine's distribution, so the files keep their absolute `auto_ontology.*` imports and each one diffs cleanly against upstream (ARCHITECTURE.md §10, §11).

Paths map one to one, except for the Alembic chain. Upstream keeps it at the repository root, and here it is inside the package, so that it ships in the wheel:

| Upstream | Here |
|---|---|
| `alembic/` | `auto_ontology/alembic/` |
| `auto_ontology/<path>` | `auto_ontology/<path>` |

Upstream's `alembic.ini` isn't vendored. `ontological/store/chains.py` configures the chain in code. Upstream's tests (`dal/tests/`, `catalog/store/tests/`, `vdb/tests/`) aren't vendored here: M2.1 vendors the DAL suite as test-only code.

## Files

| File | State |
|---|---|
| `alembic/README`, `alembic/script.py.mako`, `alembic/versions/*.py` (7 revisions, head `99d4d343dd0b`) | Unchanged |
| `alembic/env.py` | Modified: drops the `auto_ontology.env` import (python-dotenv) and uses `sqlalchemy.url` when the engine has set it |
| `__init__.py`, `infra/__init__.py` | Unchanged |
| `dal/*.py` except the four below and `model_interchange.py` | Unchanged |
| `dal/schema_version.py` | Modified: finds the chain at `auto_ontology/alembic`, not two levels above `dal/` |
| `dal/connections.py` | Modified: imports `read_secret` from `infra/keychain.py`, not `connectors/vault.py` |
| `dal/custom_analyses.py` | Modified: takes `SqlParseError` from `server/sql_errors.py`; `embed_custom_analyses` takes a LangChain `Embeddings` in place of `EmbedParams` |
| `dal/pql_analyses.py` | Modified: `embed_pql_analyses` takes a LangChain `Embeddings` in place of `EmbedParams` |
| `dal/sql_attributes.py` | Modified: takes `SqlParseError` from `server/sql_errors.py` |
| `catalog/store/*.py` | Unchanged |
| `catalog/__init__.py`, `catalog/constants.py`, `catalog/model/*.py` | Unchanged |
| `catalog/diff.py`, `catalog/normalize.py`, `catalog/query_stats.py` | Unchanged, for now: `catalog/store/` imports them. M2.3 (diff, normalize) and M2.4 (query stats) port them with fixes, and the store then imports the ports |
| `infra/postgres.py` | Modified: a `POSTGRES_HOST` that is a socket directory builds `postgresql://user@/db?host=<dir>`, with no password |
| `semantic/__init__.py`, `semantic/constants.py` | Unchanged |
| `server/__init__.py`, `server/zones/__init__.py`, `server/zones/constants.py`, `server/zones/utils.py` | Unchanged |
| `utils/sample_values.py` | Unchanged |
| `utils/__init__.py` | Modified: empty. Upstream's imports `utils.embedding` (the OpenAI client) |
| `vdb/__init__.py` | Modified: `get_vdb`, `get_data_vdb` and `get_semantic_vdb` pass `embeddings` and `vector_size` through |
| `vdb/postgres.py` | Modified: a `vector_size` up to 2,000 is indexed as `vector` with `vector_cosine_ops`, with no `halfvec` conversion (§5.4) |
| `vdb/records.py` | Modified: adds `embed_rows_into_vdb`, upstream's `utils.embedding` helper on an `Embeddings` |

Added by Ontological, in upstream's layout so the vendored imports stay one-line edits:

| File | What |
|---|---|
| `infra/keychain.py` | `read_secret(database_name)`: the connection JSON the OS keychain holds under service `Ontological`, with Vault's rules (a secret wins over the row's connection; an unreadable entry counts as none) |
| `server/sql_errors.py` | `SqlParseError`, the one class the DAL took from `server/sql_utils.py`, which loads every connector |

Each modified or added file says so under its SPDX header.

## How the engine uses it

- **Connection.** `ontological/store/dal.py` sets `POSTGRES_*` to the store's socket (no password) once the store is running, and closes the DAL's pool before the server stops. `PostgresVDB` builds its own URL from the same variables and connects with asyncpg.
- **Owners and groups.** Upstream's edit checks are in `server/owners.py`, which isn't vendored. `ontological/store/local_owner.py` stands in for it: every check allows, and rows get `LOCAL_OWNER_ID`. `dal/owners.py` and `dal/groups.py` are unchanged; the engine never calls `dal/groups.py`, whose account reads need upstream's `frontend` schema.
- **Zones.** Every DAL read takes `zone_ids=None`, the whole catalog. An empty list grants nothing.
- **Vectors.** Callers pass the embedder and its dimension: `get_vdb(embeddings=..., vector_size=...)`. The local embedder arrives in M1.5.

## For M2.1's conformance suite

Run against this package, upstream's DAL tests (no golden seed) pass except four, each from an edit above: `test_dal_surface.py` sees the new `embed_custom_analyses` and `embed_pql_analyses` signatures and the missing `model_interchange` module, and `test_migration_revision.py` looks for `alembic/` three levels up. When M2.1 vendors `server/sql_utils.py` as test-only code, it should import `SqlParseError` from `server/sql_errors.py`, so the services' `except SqlParseError` still catches the DAL's errors.

To check a file against upstream: `diff <upstream checkout>/auto_ontology/dal/connections.py engine/auto_ontology/dal/connections.py`.
