"""Hybrid search over one VDB collection: dense, BM25 and trigram rankings fused by reciprocal
rank, with a bonus for certified items (ARCHITECTURE.md §7.3).

    score = rrf(dense_rank(embedding <=> query_vec),       pgvector
                bm25_rank(name + synonyms + description),  in the engine, ontological.embed.bm25
                trgm_rank(similarity(name, query)))        pg_trgm: typos, partial names
            + certified_bonus

- It keeps upstream's filters (the collection is the VDB; `where` filters on `label` and
  `database_name`, see ontological.embed.where) and its result contract: each hit is
  `{text, metadata, _distance}`, as `PostgresVDB.retrieval` returns them, with `_distance` the
  cosine distance to the query whichever ranking found the row. `_score` is the fused score.
- The query is embedded alone through the VDB's embedder, from the caller's thread, beside any
  bulk batch in flight (§5.6, S7): there is no queue.
- The BM25 index rebuilds on every change to the collection, off the request path: the vendored
  `PostgresVDB` reports each write and delete to `on_change`, whichever instance made it.
- The trigram ranking reads the row's `name` from its metadata; rows without one aren't in it.
  Its GIN index is made on the rebuild thread once the collection's table exists.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

import psycopg
from psycopg import sql

from auto_ontology.vdb.postgres import PostgresVDB, on_change
from auto_ontology.vdb.records import content_hash
from ontological.embed import where as W
from ontological.embed.bm25 import GLOSSARY, Bm25Index, Document

log = logging.getLogger(__name__)

RRF_K = 60  # the usual constant: rank r in a list adds 1 / (RRF_K + r)
CANDIDATES = 50  # rows each ranking contributes to the fusion (S8 timed BM25 at k=50)
# About what moving from 10th to 1st in one ranking adds (1/61 - 1/70), so a certified item wins
# close calls and nothing more. The retrieval benchmark (M3.9) tunes it.
CERTIFIED_BONUS = 0.002

_NAME = sql.SQL("(langchain_metadata ->> 'name')")

Hit = dict[str, Any]


def rrf(rankings: Iterable[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal-rank fusion: each id's sum over the rankings of 1 / (k + rank), rank from 1."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, id_ in enumerate(ranking, start=1):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (k + rank)
    return scores


def is_certified(metadata: Mapping[str, Any]) -> bool:
    """A row is certified when its metadata says `certified: true` (or upstream's aggregate
    status, "certified")."""
    return metadata.get("certified") in (True, "certified")


def _vector(values: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


class HybridSearch:
    """Hybrid search over `vdb`'s collection, with a BM25 index kept current. Long-lived: make one
    per collection and `close` it at shutdown."""

    def __init__(
        self,
        vdb: PostgresVDB,
        *,
        glossary: Mapping[str, tuple[str, ...]] = GLOSSARY,
        candidates: int = CANDIDATES,
        certified_bonus: float = CERTIFIED_BONUS,
    ) -> None:
        self.vdb = vdb
        self.candidates = candidates
        self.certified_bonus = certified_bonus
        self._table = sql.Identifier(vdb.schema_name, vdb.collection_name)
        self._trgm_indexed = False  # the rebuild thread's
        self.bm25 = Bm25Index(self._load, glossary=glossary, name=f"bm25:{vdb.collection_name}")
        self._stop_listening = on_change(self._changed)
        self.bm25.invalidate()

    def close(self, timeout: float | None = None) -> None:
        self._stop_listening()
        self.bm25.close(timeout)

    def _changed(self, schema_name: str, collection_name: str) -> None:
        if (schema_name, collection_name) == (self.vdb.schema_name, self.vdb.collection_name):
            self.bm25.invalidate()

    def _connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        return psycopg.connect(self.vdb.connection_string)

    # --- the BM25 corpus, read on the rebuild thread -------------------------------------------

    def _load(self) -> list[Document]:
        with self._connect() as conn:
            regclass = conn.execute(
                "SELECT to_regclass(%s)",
                (f'"{self.vdb.schema_name}"."{self.vdb.collection_name}"',),
            ).fetchone()
            if regclass is None or regclass[0] is None:
                return []
            if not self._trgm_indexed:
                index = sql.Identifier(f"ix_trgm_{self.vdb.collection_name}_name")
                conn.execute(
                    sql.SQL(
                        "CREATE INDEX IF NOT EXISTS {} ON {} USING gin ({} gin_trgm_ops)"
                    ).format(index, self._table, _NAME)
                )
                conn.commit()
                self._trgm_indexed = True
            rows = conn.execute(
                sql.SQL(
                    "SELECT langchain_id::text, content, langchain_metadata ->> 'content_hash', "
                    "label, database_name FROM {}"
                ).format(self._table)
            ).fetchall()
        return [
            Document(
                id=id_,
                text=text or "",
                content_hash=hash_ or content_hash(text or ""),
                label=label,
                database_name=database_name,
            )
            for id_, text, hash_, label, database_name in rows
        ]

    # --- search --------------------------------------------------------------------------------

    def search(
        self, query: str, *, k: int = 10, where: Mapping[str, Any] | None = None
    ) -> list[Hit]:
        """The `k` best rows for `query` that `where` keeps, best first."""
        f = W.parse(where)
        vector = _vector(self.vdb.embeddings.embed_query(query))
        bm25 = [id_ for id_, _ in self.bm25.search(query, self.candidates, f)]
        keep = W.to_sql(f)
        params = {"v": vector, "q": query, "n": self.candidates}
        try:
            with self._connect() as conn:
                dense = self._ids(
                    conn,
                    sql.SQL(
                        "SELECT langchain_id::text FROM {t} WHERE {keep} "
                        "ORDER BY embedding <=> %(v)s::vector LIMIT %(n)s"
                    ),
                    keep,
                    params,
                )
                trgm = self._ids(
                    conn,
                    sql.SQL(
                        "SELECT langchain_id::text FROM {t} WHERE {keep} AND {name} %% %(q)s "
                        "ORDER BY similarity({name}, %(q)s) DESC, langchain_id LIMIT %(n)s"
                    ),
                    keep,
                    params,
                )
                fused = rrf([dense, bm25, trgm])
                rows = self._rows(conn, list(fused), keep, vector)
        except psycopg.errors.UndefinedTable:
            return []  # nothing written to the collection yet
        hits: list[Hit] = []
        for id_, hit in rows.items():
            certified = is_certified(cast(Mapping[str, Any], hit["metadata"]))
            hit["_score"] = fused[id_] + (self.certified_bonus if certified else 0.0)
            hits.append(hit)
        hits.sort(key=lambda h: (-h["_score"], h["_distance"], h["metadata"].get("id") or ""))
        return hits[:k]

    def _ids(
        self,
        conn: psycopg.Connection[tuple[Any, ...]],
        query: sql.SQL,
        keep: sql.Composable,
        params: Mapping[str, Any],
    ) -> list[str]:
        statement = query.format(t=self._table, keep=keep, name=_NAME)
        return [str(r[0]) for r in conn.execute(statement, params).fetchall()]

    def _rows(
        self,
        conn: psycopg.Connection[tuple[Any, ...]],
        ids: list[str],
        keep: sql.Composable,
        vector: str,
    ) -> dict[str, Hit]:
        """The rows of `ids` that still exist and `keep` still keeps (the BM25 index can be a
        rebuild behind), as upstream's hits: metadata is the JSON with the metadata columns set
        over it, as langchain-postgres returns it."""
        if not ids:
            return {}
        statement = sql.SQL(
            "SELECT langchain_id::text, content, langchain_metadata, database_name, label, "
            "embedding <=> %(v)s::vector FROM {t} "
            "WHERE {keep} AND langchain_id = ANY(%(ids)s::uuid[])"
        ).format(t=self._table, keep=keep)
        out: dict[str, Hit] = {}
        for id_, text, metadata, database_name, label, distance in conn.execute(
            statement, {"v": vector, "ids": ids}
        ).fetchall():
            meta = dict(cast(Mapping[str, Any], metadata or {}))
            meta["database_name"] = database_name
            meta["label"] = label
            out[str(id_)] = {"text": text, "metadata": meta, "_distance": float(distance)}
        return out
