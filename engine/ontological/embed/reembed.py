"""A re-embed: a database's vectors made current, after the embedding model changes or an import
writes to it (ARCHITECTURE.md §7.1, §9.3; BUILD_PLAN.md M5.1, M6.4).

Vectors from different models are never compared, and every vector's metadata names its model and
its text's hash. A re-embed syncs the database's semantic layer through `sync_database`, which
embeds every row with no current vector and deletes the rest, then replaces its data layer with
`refresh_data_layer` when a row there is another model's or its texts aren't the catalog's now.
After a model change that is everything; after an import, the texts the import created or changed.
At 10⁵ texts it takes minutes (S7), so it runs as a job:

- a row in ops.job, `reembed` for the database, with no steps: the sync is its own checkpoint,
  since a rerun embeds only the rows still missing a vector from this model;
- counted in `ActiveJobs` while it runs, so the engine doesn't idle out under it, and one per
  database at a time;
- resumed by the next run when a crash left it `running` or an error `stopped` it. An error
  stops it with the message and is raised to the caller.

The semantic layer's old vectors go before the new ones are written, so until the job ends that
database's Terms and attributes are partly missing from search, rather than ranked against
vectors of another model.
"""

# The vendored vdb is untyped.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import logging
from dataclasses import dataclass

from auto_ontology.dal.session import store
from ontological.catalog.data_layer import refresh_data_layer
from ontological.embed.embedder import LocalEmbedder
from ontological.embed.search import semantic_vdb
from ontological.embed.semantic import Synced, sync_database
from ontological.jobs import store as job_store
from ontological.jobs.active import ActiveJobs

log = logging.getLogger(__name__)

KIND = "reembed"
COLLECTIONS = ("semantic_layer", "data_objects_layer")  # in the `vdb` schema


@dataclass(frozen=True)
class Reembedded:
    database_name: str
    job_id: int
    semantic: Synced
    data_layer: int  # data-layer rows written, 0 when they were already current


def _stale_in(collection: str, model: str) -> set[str]:
    [exists] = store().query_read("SELECT to_regclass(:t) AS t", {"t": f"vdb.{collection}"})
    if exists["t"] is None:
        return set()
    rows = store().query_read(
        f'SELECT DISTINCT database_name FROM vdb."{collection}" '
        "WHERE (langchain_metadata ->> 'model') IS DISTINCT FROM :model",
        {"model": model},
    )
    return {str(r["database_name"]) for r in rows if r["database_name"] is not None}


def stale_databases(embedder: LocalEmbedder, collection: str | None = None) -> list[str]:
    """The databases with a vector from a model other than `embedder`'s, by name."""
    names: set[str] = set()
    for c in (collection,) if collection else COLLECTIONS:
        names |= _stale_in(c, embedder.model)
    return sorted(names)


def reembed_database(
    database_name: str, embedder: LocalEmbedder, *, jobs: ActiveJobs | None = None
) -> Reembedded:
    """Re-embed one database's vectors with `embedder`, as a tracked `reembed` job."""
    with (jobs or ActiveJobs()).track(KIND, database_name):
        job = job_store.open_job(KIND, database_name)
        if job is None:
            job = job_store.start_job(KIND, database_name, [])
        elif job.state != "running":
            job_store.set_state(job.id, "running")
        try:
            vdb = semantic_vdb(embedder)
            try:
                semantic = sync_database(vdb, database_name)
            finally:
                vdb.close()
            data_layer = refresh_data_layer(database_name, embedder)
        except Exception as e:
            job_store.set_state(job.id, "stopped", error=f"{type(e).__name__}: {e}")
            raise
        job_store.set_state(job.id, "done")
    log.info(
        "re-embedded %s with %s: semantic layer %s, %d data-layer rows",
        database_name,
        embedder.model,
        semantic,
        data_layer,
    )
    return Reembedded(database_name, job.id, semantic, data_layer)
