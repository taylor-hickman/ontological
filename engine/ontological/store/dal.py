"""Point upstream's DAL and vector store at the bundled store (ARCHITECTURE.md §5.3, §5.4).

Both build their URL from `POSTGRES_*` (`auto_ontology/infra/postgres.py`). A socket directory as
`POSTGRES_HOST` gives `postgresql://ontological@/ontological?host=<pgsocket>`, with no password:
the server listens only on the socket, and trusts it.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

import auto_ontology.dal
from ontological.store import pg


def environ(layout: pg.Layout) -> dict[str, str]:
    """The `POSTGRES_*` variables that name the store."""
    return {
        "POSTGRES_HOST": str(layout.pgsocket),
        "POSTGRES_PORT": str(pg.PORT),
        "POSTGRES_USER": pg.USER,
        "POSTGRES_DATABASE": pg.DATABASE,
    }


def connect(layout: pg.Layout, env: MutableMapping[str, str] = os.environ) -> None:
    """Set `POSTGRES_*` to the store, and drop a pool built for any other URL."""
    env.pop("POSTGRES_PASSWORD", None)
    env.update(environ(layout))
    auto_ontology.dal.close_store()


def close() -> None:
    """Release the DAL's pooled connections. A `PostgresVDB` closes its own."""
    auto_ontology.dal.close_store()
