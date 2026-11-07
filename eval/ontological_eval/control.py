"""The control arm: a schema-only baseline (BUILD_PLAN.md, Scoring and runs).

One structured call per question, with the question, its evidence and a dump of the database's
schema, no tools, on the same model and effort as the agent. Whatever it scores is what the
model does from the schema alone, so the agent's lift is measured against it.

It follows ontology-sql-eval's schema-only arm (`ontology_sql_eval/gym/ddl.py` and `tasks.py`
at `ec41b2c`, Apache-2.0), which keeps itself close to NeMo Gym's `bird_sql`:
- The dump is the database's own DDL: `CREATE TABLE` statements read back from a BIRD file's
  `sqlite_master`, wrapped in `BEGIN TRANSACTION;`/`COMMIT;`, and WWI's shipped `ddl/*.sql`
  files in sorted order. BIRD's column descriptions aren't added, as Ontological doesn't read
  them either.
- The user turn is theirs: the question, the evidence, the dialect, then the dump. Their system
  prompt asks for a fenced `sql` block; this one asks for the `sql` field of a structured
  answer, and keeps their projection rule, which the agent also has.
- Its SQL runs natively, where the gold SQL runs, as their arm's does, so the control needs no
  engine and writes the dialect its schema is in.

The answers are stored per model and effort (`runs.control_path`), and a run asks only about the
questions that have none, so the control runs once per model per set (BUILD_PLAN.md).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from .home import bird_dir, wwi_dir
from .questions import Question

SYSTEM = (
    "You write SQL. Put a single query that answers the user's question in the `sql` field.\n"
    "Return exactly the requested output fields and NO others."
)


class ControlAnswer(BaseModel):
    sql: str = Field(description="One query that answers the question")


def dialect(q: Question) -> str:
    return "PostgreSQL" if q.source == "wwi" else "SQLite"


def sqlite_schema_dump(path: Path) -> str:
    """The `CREATE TABLE` dump of a SQLite file, in `sqlite_master` (creation) order."""
    if not path.exists():
        raise FileNotFoundError(f"no SQLite database at {path}")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        conn.text_factory = _decode
        rows = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    body = "\n".join(str(r[0]).strip().rstrip(";") + ";" for r in rows)
    return f"BEGIN TRANSACTION;\n{body}\nCOMMIT;"


def _decode(b: bytes) -> str:
    return b.decode(errors="ignore")


def postgres_schema_dump(ddl_dir: Path) -> str:
    """The `*.sql` DDL files a Postgres dataset ships, concatenated in sorted order."""
    files = sorted(ddl_dir.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no .sql files under {ddl_dir}")
    return "\n\n".join(f"-- {f.name}\n{f.read_text().strip()}" for f in files)


def schema_dump(home: Path, q: Question) -> str:
    if q.source == "wwi":
        return postgres_schema_dump(wwi_dir(home) / "ddl")
    return sqlite_schema_dump(bird_dir(home) / f"{q.db}.sqlite")


def user_prompt(q: Question, schema: str) -> str:
    parts = [f"### Question\n{q.question}"]
    if q.evidence:
        parts.append(f"### Evidence\n{q.evidence}")
    parts.append(f"Write a single {dialect(q)} query that answers the question.")
    parts.append(
        f"The following is a SQL dump that describes the database and the tables in it.\n{schema}"
    )
    return "\n\n".join(parts)
