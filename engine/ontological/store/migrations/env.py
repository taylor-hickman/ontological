"""Alembic environment for Ontological's chain: the `app` and `ops` schemas (ARCHITECTURE.md §5.2).

Upstream's vendored chain (auto_ontology/alembic) owns `public` and keeps its version table at
`public.alembic_version`. This chain is independent of it: its version table is
`ops.alembic_version`, so new upstream revisions can be vendored without renumbering anything
here. Every object this chain creates is schema-qualified, and it never touches `public` or
`vdb`.

`ontological.store.chains` sets `sqlalchemy.url` to the store's socket URL; there is no
alembic.ini.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

SCHEMAS = ("app", "ops")
VERSION_SCHEMA = "ops"

config = context.config


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Autogenerate looks only at this chain's schemas, so it never proposes dropping upstream's
    tables in `public` or the vectors in `vdb`."""
    schema = getattr(obj, "schema", None) or getattr(getattr(obj, "table", None), "schema", None)
    return schema in SCHEMAS


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Alembic creates its version table before the first revision runs, so the schema that
        # holds it has to exist first. 0001 creates `app`.
        connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {VERSION_SCHEMA}")
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=None,
            include_object=include_object,
            include_schemas=True,
            version_table="alembic_version",
            version_table_schema=VERSION_SCHEMA,
            # One transaction per revision, so a revision that fails rolls back on its own and
            # the ones before it stay committed; the pre-migration dump undoes those (§5.3).
            transaction_per_migration=True,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Ontological's chain runs online only, against the store")
run_migrations_online()
