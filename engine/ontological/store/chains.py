"""The store's two Alembic chains (ARCHITECTURE.md §5.2), and running them.

Upstream's chain, vendored at auto_ontology/alembic, owns `public` and keeps its version table at
`public.alembic_version`. Ontological's, at ontological/store/migrations, owns `app` and `ops`
and keeps its version table at `ops.alembic_version`. They run in that order. Neither has an
alembic.ini: each is configured here with its script directory and the store's URL.

`pg.migrate` wraps these in the pre-migration dump and the restore on failure (§5.3).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool

import auto_ontology


@dataclass(frozen=True)
class Chain:
    name: str
    scripts: Path  # the chain's script directory: env.py, versions/
    version_schema: str | None  # the schema of its alembic_version; None is `public`


UPSTREAM = Chain("upstream", Path(auto_ontology.__file__).parent / "alembic", None)
ONTOLOGICAL = Chain("ontological", Path(__file__).with_name("migrations"), "ops")
CHAINS = (UPSTREAM, ONTOLOGICAL)


def config(chain: Chain, url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(chain.scripts))
    # Config goes through ConfigParser interpolation, and the socket URL percent-encodes the
    # space in `Application Support`.
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def pending(url: str, chains: Sequence[Chain] = CHAINS) -> list[str]:
    """The names of the chains whose head revision the store hasn't reached, in run order."""
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            behind: list[str] = []
            for chain in chains:
                heads = set(ScriptDirectory.from_config(config(chain, url)).get_heads())
                context = MigrationContext.configure(
                    connection, opts={"version_table_schema": chain.version_schema}
                )
                if set(context.get_current_heads()) != heads:
                    behind.append(chain.name)
            return behind
    finally:
        engine.dispose()


def upgrade(url: str, chains: Sequence[Chain] = CHAINS) -> None:
    """Bring every chain to its head, in order. A revision that fails raises, and the revisions
    that committed before it stay committed."""
    for chain in chains:
        command.upgrade(config(chain, url), "head")
