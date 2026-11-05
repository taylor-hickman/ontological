"""The eval's questions and their gold SQL, read from the downloaded data (BUILD_PLAN.md, Eval set).

WWI's question IDs (1-41) and BIRD Mini-Dev's (0-499) overlap, so a question is named by its
source and its ID together. BIRD questions keep their `evidence` text.

Two of WWI's gold queries don't run as published, and their fixes live here, applied on load, so
the downloaded evaluation.json stays as its pinned hash says. Five more return no rows on the
data: those are left out of every set (WWI_DROPPED), with the reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .home import bird_dir, wwi_dir

Source = Literal["wwi", "bird"]

# The four Mini-Dev databases the eval uses.
BIRD_DATABASES = ("financial", "california_schools", "formula_1", "debit_card_specializing")

WWI_FIXED_SQL = {
    # Published with its newlines stripped, which runs words together ("total_quantityfrom").
    27: """with northamericacountries as (
    select countryid from application.countries where continent = 'North America'
),
northamericacustomers as (
    select customers.customerid
    from sales.customers
    join application.cities on customers.deliverycityid = cities.cityid
    join application.stateprovinces on cities.stateprovinceid = stateprovinces.stateprovinceid
    where stateprovinces.countryid in (select countryid from northamericacountries)
),
northamericasales as (
    select invoicelines.stockitemid, sum(invoicelines.quantity) as total_quantity
    from sales.invoicelines
    join sales.invoices on invoicelines.invoiceid = invoices.invoiceid
    where invoices.customerid in (select customerid from northamericacustomers)
    group by invoicelines.stockitemid
)
select northamericasales.stockitemid, northamericasales.total_quantity
from northamericasales
order by northamericasales.total_quantity desc
limit 4;""",
    # Selects ct.customerid without grouping by it, which Postgres rejects. Customer names are
    # unique in WWI, so grouping by both keeps the published query's meaning.
    4: """select
    c.customername as customername,
    ct.customerid as customerid,
    avg(ct.transactionamount) as averagetransactionamount
from sales.customertransactions ct
left join sales.customers c on ct.customerid = c.customerid
where ct.isfinalized = true
group by c.customername, ct.customerid;""",
}

_SPECIAL_DEALS = (
    "joins sales.specialdeals on stockitemid, which is NULL in both of WWI's special deals "
    "(they're keyed by buying group and stock group), so the gold result is empty"
)
WWI_DROPPED = {
    1: "asks about Black Friday 2024; WWI's orders end on 2016-05-31, so the gold result is empty",
    7: _SPECIAL_DEALS + " (one row, all NULL)",
    24: _SPECIAL_DEALS,
    36: _SPECIAL_DEALS,
    41: _SPECIAL_DEALS,
}


@dataclass(frozen=True)
class Question:
    source: Source
    id: int
    db: str  # "wideworldimporters", or the BIRD database's db_id
    question: str
    evidence: str
    sql: str
    difficulty: str  # "simple", "moderate" or "challenging"; "" for WWI

    @property
    def key(self) -> tuple[Source, int]:
        return (self.source, self.id)


def load_wwi(home: Path) -> list[Question]:
    """All 41 WWI questions, the fixes in WWI_FIXED_SQL applied. WWI_DROPPED ones included."""
    raw: list[dict[str, Any]] = json.loads((wwi_dir(home) / "evaluation.json").read_text())
    return [
        Question(
            source="wwi",
            id=int(r["question_id"]),
            db="wideworldimporters",
            question=r["question"],
            evidence=r.get("evidence") or "",
            sql=WWI_FIXED_SQL.get(int(r["question_id"])) or r["SQL"],
            difficulty=r.get("difficulty") or "",
        )
        for r in raw
    ]


def load_bird(home: Path) -> list[Question]:
    """All 500 Mini-Dev questions, from every database: the difficulty mix is Mini-Dev's own."""
    lines = (bird_dir(home) / "mini_dev_sqlite.jsonl").read_text().splitlines()
    out: list[Question] = []
    for line in lines:
        if not line.strip():
            continue
        r: dict[str, Any] = json.loads(line)
        out.append(
            Question(
                source="bird",
                id=int(r["question_id"]),
                db=r["db_id"],
                question=r["question"],
                evidence=r.get("evidence") or "",
                sql=r["SQL"],
                difficulty=r["difficulty"],
            )
        )
    return out


def load(home: Path) -> dict[tuple[Source, int], Question]:
    return {q.key: q for q in [*load_wwi(home), *load_bird(home)]}
