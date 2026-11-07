"""Packed calls: several small tables to one call per stage (ARCHITECTURE.md §6.4).

A pack's prompt is each table's own prompt, one after another, every one headed by its
`Table:` line, and its system prompt is the stage's with a note on answering per table. The
answer is one entry per table (`models.Packed*`), matched back to the tables by that heading. A
stage asks a table on its own, with upstream's prompt unchanged, when it is the only one of its
pack with anything to ask, and when a pack's answer leaves it out.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

from pydantic import BaseModel

from ontological.compile.gateway import Structured, ask

# Packed prompts are Ontological's, so they carry a version of their own beside upstream's.
PROMPT_VERSION = "2a24f5d+pack.1"

NOTE = """

This request covers several tables. Each one starts with its own "Table:" line, followed by \
that table's details. Apply the instructions above to each table on its own, and return one \
entry per table in `tables`, with `table` set to the name exactly as written after "Table:"."""


def system(stage_system: str) -> str:
    return stage_system + NOTE


def join(prompts: Iterable[str]) -> str:
    return "\n".join(p.rstrip("\n") + "\n" for p in prompts)


class Entry(Protocol):
    @property
    def table(self) -> str: ...


def match[E](
    entries: Sequence[E], headings: Sequence[str], key: Callable[[E], str]
) -> dict[str, E]:
    """Each heading's entry, by the name `key` reads from it: the first that names it exactly,
    else the first that names it ignoring case and surrounding space. A heading no entry names
    is left out."""
    exact: dict[str, E] = {}
    folded: dict[str, E] = {}
    for entry in entries:
        exact.setdefault(key(entry), entry)
        folded.setdefault(key(entry).strip().casefold(), entry)
    found: dict[str, E] = {}
    for heading in headings:
        entry = exact.get(heading) or folded.get(heading.strip().casefold())
        if entry is not None:
            found[heading] = entry
    return found


async def ask_pack[E: Entry, P: BaseModel](
    gateway: Structured,
    task: str,
    stage_system: str,
    sections: Sequence[tuple[str, str]],
    schema: type[P],
    entries: Callable[[P], Sequence[E]],
) -> dict[str, E]:
    """One call for `sections`, each a table's heading and its prompt; each heading's entry in
    the answer. An invalid answer gives none, so every table is asked on its own. Raises
    `ClaudeUnavailable` when Claude doesn't answer."""
    result = await ask(
        gateway,
        task,
        system(stage_system),
        join(prompt for _, prompt in sections),
        schema,
        PROMPT_VERSION,
    )
    if result is None:
        return {}
    return match(entries(result), [heading for heading, _ in sections], lambda e: e.table)
