"""The BM25 index: identifier-aware tokens and the rebuild policy (ARCHITECTURE.md §7.3, S8)."""

from __future__ import annotations

import threading
from collections.abc import Sequence

import pytest

from auto_ontology.vdb.records import content_hash
from ontological.embed import where as W
from ontological.embed.bm25 import Bm25Index, Document, Snapshot, tokenize

TIMEOUT = 10.0


def doc(id_: str, text: str, label: str | None = "Column", db: str | None = "chinook") -> Document:
    return Document(id_, text, content_hash(text), label, db)


def snapshot(docs: Sequence[Document]) -> Snapshot:
    return Snapshot.build(docs, [tokenize(d.text) for d in docs])


def top(s: Snapshot, query: str, k: int = 5, where: W.Filter = W.EVERYTHING) -> list[str]:
    return [id_ for id_, _ in s.search(tokenize(query), k, where)]


# --- tokens --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("cust_acct_no", ["customer", "account", "number"]),
        ("ZorblaxInvoiceLineID", ["zorblax", "invoice", "line", "id"]),
        ("QUUXTREL_SKU_QTY", ["quuxtrel", "stock", "keeping", "unit", "quantity"]),
        ("HTTPServer.addressLine2", ["http", "server", "address", "line", "2"]),
        ("The total of the invoice", ["total", "invoice"]),
    ],
)
def test_identifiers_split_and_glossary_words_expand(text: str, tokens: list[str]) -> None:
    assert tokenize(text) == tokens


def test_a_query_and_the_identifier_it_names_tokenize_alike() -> None:
    assert tokenize("customer account number") == tokenize("billing.cust_acct_no")[1:]
    assert tokenize("quuxtrel SKU qty") == tokenize("quuxtrel stock keeping unit quantity")


def test_the_glossary_is_a_parameter() -> None:
    assert tokenize("arr_by_region", {"arr": ("annual", "recurring", "revenue")}) == [
        "annual",
        "recurring",
        "revenue",
        "region",
    ]
    assert tokenize("arr_by_region", {}) == ["arr", "region"]


# --- ranking -------------------------------------------------------------------------------------

PLANTED = [
    doc("acct", "billing_master.cust_acct_no"),
    doc("line", "zorblax_line.ZorblaxInvoiceLineID Zorblax invoice line identifier"),
    doc("qty", "quuxtrel_items.QUUXTREL_SKU_QTY Units on hand."),
]
DISTRACTORS = [
    doc("d1", "customer.first_name The customer's first name."),
    doc("d2", "invoice.total The amount billed on the invoice."),
    doc("d3", "account.created_at When the account was opened."),
    doc("d4", "invoice_line.unit_price Price of one unit on the line."),
    doc("d5", "inventory.on_hand_qty Units in the warehouse."),
]


@pytest.mark.parametrize(
    ("query", "first"),
    [
        ("customer account number", "acct"),
        ("cust_acct_no", "acct"),
        ("zorblax invoice line id", "line"),
        ("ZorblaxInvoiceLineID", "line"),
        ("quuxtrel SKU qty", "qty"),
        ("quuxtrel stock keeping unit quantity", "qty"),
    ],
)
def test_planted_documents_rank_first(query: str, first: str) -> None:
    assert top(snapshot(PLANTED + DISTRACTORS), query)[0] == first


def test_only_documents_sharing_a_token_rank_and_k_caps_them() -> None:
    s = snapshot(PLANTED + DISTRACTORS)
    assert top(s, "zebra giraffe") == []
    assert top(s, "") == []
    assert len(top(s, "invoice", k=2)) == 2
    assert set(top(s, "invoice", k=10)) == {"line", "d2", "d4"}


def test_an_empty_index_finds_nothing() -> None:
    assert top(snapshot([]), "invoice") == []


def test_filters_apply_with_sqls_null_rules() -> None:
    s = snapshot(
        [
            doc("a", "invoice total", label="Column", db="chinook"),
            doc("b", "invoice total", label="Table", db="chinook"),
            doc("c", "invoice total", label="Column", db="pagila"),
            doc("d", "invoice total", label="Column", db=None),
        ]
    )

    def hits(where: dict[str, object]) -> set[str]:
        return set(top(s, "invoice", 10, W.parse(where)))

    assert hits({}) == {"a", "b", "c", "d"}
    assert hits({"label": "Table"}) == {"b"}
    assert hits({"database_name": ["pagila", "chinook"], "label": "Column"}) == {"a", "c"}
    assert hits({"database_name": {"$ne": "chinook"}}) == {"c"}  # d is NULL: no match
    assert hits({"database_name": {"$nin": ["pagila"]}}) == {"a", "b"}
    assert hits({"$or": [{"label": "Table"}, {"database_name": "pagila"}]}) == {"b", "c"}
    assert hits({"label": "Nothing"}) == set()


@pytest.mark.parametrize(
    "where",
    [
        {"name": "x"},
        {"label": {"$like": "x%"}},
        {"label": {"$eq": "a", "$ne": "b"}},
        {"label": {"$in": "a"}},
        {"label": {"$eq": ["a"]}},
        {"label": None},
        {"$and": {"label": "a"}},
    ],
)
def test_a_filter_upstream_does_not_use_is_refused(where: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        W.parse(where)


# --- rebuilds ------------------------------------------------------------------------------------


class Store:
    """A load function whose documents a test changes, and which can hold a rebuild in `load`
    until the test releases it."""

    def __init__(self, docs: Sequence[Document]) -> None:
        self.docs = list(docs)
        self.hold = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.loads = 0
        self.inside = 0
        self.most_inside = 0
        self.fail = False
        self._lock = threading.Lock()

    def load(self) -> list[Document]:
        with self._lock:
            self.loads += 1
            self.inside += 1
            self.most_inside = max(self.most_inside, self.inside)
        try:
            if self.hold:
                self.entered.set()
                assert self.release.wait(TIMEOUT)
            if self.fail:
                raise RuntimeError("store unreachable")
            return list(self.docs)
        finally:
            with self._lock:
                self.inside -= 1


def built(store: Store) -> Bm25Index:
    index = Bm25Index(store.load)
    index.invalidate()
    assert index.wait(TIMEOUT)
    return index


def test_a_rebuild_runs_off_the_callers_thread_and_swaps_in_whole() -> None:
    store = Store([doc("a", "invoice total")])
    index = built(store)
    store.hold = True
    store.docs = [doc("b", "invoice line")]

    index.invalidate()  # returns while the rebuild is held in load
    assert store.entered.wait(TIMEOUT)
    assert [i for i, _ in index.search("invoice", 5)] == ["a"]  # the old index, whole

    store.release.set()
    assert index.wait(TIMEOUT)
    assert [i for i, _ in index.search("invoice", 5)] == ["b"]
    index.close()


def test_changes_during_a_rebuild_queue_one_more_rebuild() -> None:
    store = Store([doc("a", "invoice total")])
    index = built(store)
    assert index.rebuilds == 1
    store.hold = True

    index.invalidate()
    assert store.entered.wait(TIMEOUT)
    store.docs = [doc(f"d{n}", f"invoice {n}") for n in range(20)]
    for _ in range(20):  # a burst of compile writes
        index.invalidate()
    store.hold = False
    store.release.set()

    assert index.wait(TIMEOUT)
    assert index.rebuilds == 3  # the first, the held one and one more: not one per write
    assert store.most_inside == 1
    assert len(index.search("invoice", 50)) == 20  # the queued rebuild saw the last write
    index.close()


def test_a_rebuild_tokenizes_only_documents_whose_content_hash_changed() -> None:
    docs = [doc(f"d{n}", f"invoice line {n}") for n in range(10)]
    store = Store(docs)
    index = built(store)
    assert index.tokenized == 10

    store.docs = [*docs[:9], doc("d9", "invoice total, rewritten"), doc("new", "customer name")]
    index.invalidate()
    assert index.wait(TIMEOUT)
    assert index.tokenized == 12
    assert [i for i, _ in index.search("rewritten", 5)] == ["d9"]

    # A row whose text came back to an earlier one is tokenized again: the cache keeps only the
    # hashes the last rebuild saw.
    store.docs = docs
    index.invalidate()
    assert index.wait(TIMEOUT)
    assert index.tokenized == 13
    index.close()


def test_a_new_glossary_retokenizes_everything() -> None:
    store = Store([doc("a", "arr_by_region"), doc("b", "revenue")])
    index = built(store)
    assert [i for i, _ in index.search("annual recurring", 5)] == ["a"]

    index.set_glossary({})
    assert index.wait(TIMEOUT)
    assert index.tokenized == 4
    assert index.search("annual recurring", 5) == []
    assert [i for i, _ in index.search("arr", 5)] == ["a"]
    index.close()


def test_a_failed_rebuild_keeps_the_previous_index() -> None:
    store = Store([doc("a", "invoice total")])
    index = built(store)
    store.fail = True
    index.invalidate()
    assert index.wait(TIMEOUT)
    assert index.rebuilds == 2
    assert [i for i, _ in index.search("invoice", 5)] == ["a"]

    store.fail = False
    store.docs = [doc("b", "invoice line")]
    index.invalidate()
    assert index.wait(TIMEOUT)
    assert [i for i, _ in index.search("invoice", 5)] == ["b"]
    index.close()


def test_a_closed_index_starts_no_rebuild() -> None:
    store = Store([doc("a", "invoice total")])
    index = built(store)
    index.close()
    index.invalidate()
    assert index.wait(TIMEOUT)
    assert store.loads == 1


def test_searches_before_the_first_rebuild_find_nothing() -> None:
    index = Bm25Index(Store([doc("a", "invoice")]).load)
    assert index.search("invoice", 5) == []
