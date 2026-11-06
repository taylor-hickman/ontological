"""The in-engine BM25 index over one VDB collection (ARCHITECTURE.md §7.3, §5.6, spike S8).

- **Identifier-aware tokens.** `tokenize` splits on anything that isn't a letter or digit, then
  on camelCase and PascalCase, lowercases, expands glossary abbreviations and acronyms and drops
  stopwords. Queries go through the same function, so `cust_acct_no` and "customer account
  number" meet in the middle.
- **Rebuilt off the request path, swapped in atomically.** `invalidate` asks for a rebuild and
  returns at once. A rebuild runs on a thread of its own and replaces the index with one
  reference assignment, so a search sees the old index or the new one, never a partly built one.
- **One rebuild at a time, at most one queued.** A change during a rebuild marks the index
  stale, and one more rebuild runs after the current one: a burst of writes costs two rebuilds,
  not one per write.
- **Tokens are cached by `content_hash`**, so a rebuild tokenizes only the documents whose text
  changed (about half of a full rebuild's time in S8). The cache keeps only the hashes the last
  rebuild saw.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import bm25s  # pyright: ignore[reportMissingTypeStubs]
import numpy as np
from numpy.typing import NDArray

from ontological.embed import where as W

log = logging.getLogger(__name__)

# Abbreviations and acronyms seen in identifiers, mapped to the words a person would type. The
# built-in list until Settings -> Glossary edits it (§9.1); `Bm25Index.set_glossary` takes the
# user's.
GLOSSARY: Mapping[str, tuple[str, ...]] = {
    "acct": ("account",),
    "addr": ("address",),
    "amt": ("amount",),
    "arr": ("annual", "recurring", "revenue"),
    "avg": ("average",),
    "bal": ("balance",),
    "cat": ("category",),
    "cnt": ("count",),
    "cust": ("customer",),
    "curr": ("currency",),
    "desc": ("description",),
    "dept": ("department",),
    "dob": ("date", "birth"),
    "dt": ("date",),
    "emp": ("employee",),
    "gmv": ("gross", "merchandise", "value"),
    "inv": ("invoice",),
    "ltv": ("lifetime", "value"),
    "mrr": ("monthly", "recurring", "revenue"),
    "no": ("number",),
    "num": ("number",),
    "ord": ("order",),
    "pct": ("percent",),
    "po": ("purchase", "order"),
    "prod": ("product",),
    "qty": ("quantity",),
    "rev": ("revenue",),
    "sku": ("stock", "keeping", "unit"),
    "ts": ("timestamp",),
    "txn": ("transaction",),
    "uom": ("unit", "measure"),
    "usr": ("user",),
    "wh": ("warehouse",),
}

_STOPWORDS = (
    "a an and are as at be by for from has in is it its of on or that the this to was "
    "were will with which each per all any one when where who how not"
)
STOPWORDS = frozenset(_STOPWORDS.split())

# Runs of letters and digits, so `_`, `.`, `-` and spaces split.
_WORD = re.compile(r"[A-Za-z0-9]+")
# camelCase and PascalCase parts: `InvoiceLineID` -> Invoice, Line, ID; `HTTPServer` -> HTTP,
# Server; `addressLine2` -> address, Line, 2.
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def tokenize(text: str, glossary: Mapping[str, tuple[str, ...]] = GLOSSARY) -> list[str]:
    """Identifier-aware tokens. Glossary expansion comes before the stopword check, so the `no`
    in `cust_acct_no` becomes "number"."""
    out: list[str] = []
    for word in _WORD.findall(text):
        for part in _CAMEL.findall(word):
            low = part.lower()
            expansion = glossary.get(low)
            if expansion is not None:
                out.extend(expansion)
            elif low not in STOPWORDS:
                out.append(low)
    return out


@dataclass(frozen=True)
class Document:
    """One VDB row as the index sees it."""

    id: str
    text: str
    content_hash: str
    label: str | None
    database_name: str | None


Scores = NDArray[np.float32]


@dataclass(frozen=True)
class Snapshot:
    """A built index and its documents' ids and filter columns. Never changed once built."""

    ids: tuple[str, ...] = ()
    columns: Mapping[str, W.Column] = field(default_factory=dict[str, W.Column])
    retriever: Any = None  # bm25s.BM25, untyped

    @classmethod
    def build(cls, docs: Sequence[Document], tokens: Sequence[list[str]]) -> Snapshot:
        if not docs:
            return cls()
        retriever = bm25s.BM25()
        retriever.index(list(tokens), show_progress=False)  # pyright: ignore[reportUnknownMemberType]
        return cls(
            ids=tuple(d.id for d in docs),
            columns={
                "label": W.Column.of([d.label for d in docs]),
                "database_name": W.Column.of([d.database_name for d in docs]),
            },
            retriever=retriever,
        )

    def search(self, tokens: Sequence[str], k: int, f: W.Filter) -> list[tuple[str, float]]:
        """The `k` best documents that `f` keeps and that share a token with the query, best
        first, with their BM25 scores."""
        if self.retriever is None:
            return []
        vocab = cast(Mapping[str, int], self.retriever.vocab_dict)
        known = [t for t in tokens if t and t in vocab]
        if not known:
            return []
        keep = None if f == W.EVERYTHING else W.mask(f, self.columns, len(self.ids))
        scores = cast(Scores, self.retriever.get_scores(known, weight_mask=keep))
        hits = np.flatnonzero(scores > 0)
        if len(hits) > k:
            hits = hits[np.argpartition(-scores[hits], k - 1)[:k]]
        order = sorted(hits.tolist(), key=lambda i: (-float(scores[i]), i))
        return [(self.ids[i], float(scores[i])) for i in order]


class Bm25Index:
    """BM25 over the documents `load` returns, kept current by `invalidate`.

    `load` reads every document of the collection; it runs on the rebuild thread. Until the first
    rebuild finishes, searches find nothing.
    """

    def __init__(
        self,
        load: Callable[[], Sequence[Document]],
        *,
        glossary: Mapping[str, tuple[str, ...]] = GLOSSARY,
        name: str = "bm25",
    ) -> None:
        self._load = load
        self._glossary = dict(glossary)
        self._name = name
        self._snapshot = Snapshot()
        # content_hash -> tokens, and the glossary they came from; the rebuild thread's alone.
        self._tokens: dict[str, list[str]] = {}
        self._tokens_glossary: Mapping[str, tuple[str, ...]] | None = None
        self._lock = threading.Lock()
        self._running = False
        self._stale = False
        self._closed = False
        self._idle = threading.Event()
        self._idle.set()
        self.rebuilds = 0  # finished rebuilds, failed ones included
        self.tokenized = 0  # documents tokenized, over every rebuild

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def glossary(self) -> Mapping[str, tuple[str, ...]]:
        return self._glossary

    def search(self, query: str, k: int, f: W.Filter | None = None) -> list[tuple[str, float]]:
        snapshot = self._snapshot  # one read: the whole search uses this index
        return snapshot.search(tokenize(query, self._glossary), k, f or W.EVERYTHING)

    # --- rebuilds ------------------------------------------------------------------------------

    def invalidate(self) -> None:
        """Rebuild after a change. Returns at once: starts a rebuild if none is running, or queues
        one more after the running one."""
        with self._lock:
            if self._closed:
                return
            if self._running:
                self._stale = True
                return
            self._running = True
            self._idle.clear()
        threading.Thread(target=self._run, name=f"{self._name}-rebuild", daemon=True).start()

    def set_glossary(self, glossary: Mapping[str, tuple[str, ...]]) -> None:
        """Tokenize with `glossary` from the next rebuild on, which this starts. Every document is
        tokenized again, since cached tokens came from the old glossary."""
        with self._lock:
            self._glossary = dict(glossary)
        self.invalidate()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until no rebuild is running or queued; False if `timeout` ran out first."""
        return self._idle.wait(timeout)

    def close(self, timeout: float | None = None) -> None:
        """Start no more rebuilds, and wait for the running one."""
        with self._lock:
            self._closed = True
            self._stale = False
        self.wait(timeout)

    def _run(self) -> None:
        while True:
            try:
                self._rebuild()
            except Exception:
                log.exception("%s: rebuild failed; searches keep the previous index", self._name)
            with self._lock:
                self.rebuilds += 1
                if not self._stale:
                    self._running = False
                    self._idle.set()
                    return
                self._stale = False

    def _rebuild(self) -> None:
        with self._lock:
            glossary = self._glossary
        docs = self._load()
        if glossary is not self._tokens_glossary:
            self._tokens = {}
            self._tokens_glossary = glossary
        tokens: dict[str, list[str]] = {}
        for d in docs:
            if d.content_hash not in tokens:
                cached = self._tokens.get(d.content_hash)
                if cached is None:
                    cached = tokenize(d.text, glossary)
                    self.tokenized += 1
                tokens[d.content_hash] = cached
        self._tokens = tokens
        self._snapshot = Snapshot.build(docs, [tokens[d.content_hash] for d in docs])
        log.debug("%s: rebuilt over %d documents", self._name, len(docs))
