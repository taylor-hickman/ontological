"""ontological, the command line (BUILD_PLAN.md M1.8).

    ontological ask "<question>" --source NAME [--no-critic] [--app-data DIR]
                                 [the engine's options, such as --pg-bin DIR]

`ask` answers one question from the database NAME with the gated agent, which uses Claude quota.
It attaches to the engine engine.json names, or starts one headless as the MCP shim does, and
holds a lease while it waits for `POST /ask`. Then it prints the steps the agent took, the SQL
`validate_sql` passed, the rows `run_sql` returned, the answer, and whether the post-run check
verified it. Every option it doesn't know goes to the engine it starts.

It exits 0 for a verified answer, 2 for an unverified one or none, and 1 when it couldn't ask.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import anyio
import httpx

from ontological.answer.service import Asked, Ran
from ontological.engine import main as engine_main
from ontological.mcp.link import EngineLink, EngineUnavailable, hold_engine

CLIENT = "ontological ask"
CONNECT_TIMEOUT_S = 10.0
ARGS_WIDTH = 100  # a step's arguments, on one line
CELL_WIDTH = 40
ROWS_SHOWN = 50


class AskFailed(Exception):
    """The engine couldn't answer: the message says why."""


async def ask_engine(
    app_data: Path, start: Sequence[str], question: str, database: str, *, critic: bool
) -> Asked:
    """`POST /ask` on the engine, holding a lease until it answers. The session can run for
    minutes, so the request has no read timeout. Errors are caught inside `hold_engine`, whose
    task group would wrap them in an ExceptionGroup."""
    async with hold_engine(app_data, start, client=CLIENT) as link:
        r = await _post(link, question, database, critic=critic)
    if isinstance(r, str):
        raise AskFailed(r)
    if r.status_code != 200:
        raise AskFailed(f"the engine answered {r.status_code}: {_detail(r)}")
    return Asked.model_validate(r.json())


async def _post(
    link: EngineLink, question: str, database: str, *, critic: bool
) -> httpx.Response | str:
    """The engine's response, or why there is none."""
    try:
        info = await link.engine()
    except EngineUnavailable as e:
        return f"no engine: {e}"
    print(f"Asking {database}…", file=sys.stderr, flush=True)
    timeout = httpx.Timeout(CONNECT_TIMEOUT_S, read=None)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as http:
        try:
            return await http.post(
                f"http://127.0.0.1:{info.port}/ask",
                headers={"Authorization": f"Bearer {info.token}"},
                json={"question": question, "database": database, "critic": critic},
            )
        except httpx.TransportError as e:
            return f"the engine stopped answering: {e}"


def _detail(r: httpx.Response) -> str:
    try:
        body: Any = r.json()
    except ValueError:
        return r.text
    if isinstance(body, dict):
        return str(cast(dict[str, Any], body).get("detail", body))
    return str(body)


# --- printing ----------------------------------------------------------------------------------


def render(asked: Asked) -> str:
    out: list[str] = ["Steps"]
    if not asked.steps:
        out.append("  none")
    for n, step in enumerate(asked.steps, 1):
        args = _clip(json.dumps(step.args, ensure_ascii=False, default=str), ARGS_WIDTH)
        out.append(f"  {n}. {step.name} {args}")
        if step.is_error:
            out.append(f"     error: {_first_line(step.text)}")

    out += ["", *_sql(asked)]
    if asked.result is not None:
        out += ["", *_rows(asked.result)]

    out += ["", "Answer"]
    answer = asked.answer
    if answer is None:
        out.append("  none")
    else:
        out.append(f"  {answer.answer}")
        out += [f"  assumed: {a}" for a in answer.assumptions]
        if not answer.answerable:
            out.append(f"  not answerable; missing: {', '.join(answer.missing) or 'unsaid'}")

    out.append("")
    if asked.verified:
        out.append("Verified")
    else:
        out.append(f"Unverified: {asked.reason or 'no reason given'}")
    if asked.error:
        out.append(f"Session {asked.outcome or 'failed'}: {asked.error}")
    if asked.answered_model:
        out.append(f"Model: {asked.answered_model}")
    return "\n".join(out)


def _sql(asked: Asked) -> list[str]:
    if asked.sql is not None:
        return ["SQL (validated)", *_indent(asked.sql)]
    if asked.answer is not None and asked.answer.sql:
        return ["SQL (not validated)", *_indent(asked.answer.sql)]
    return ["SQL: none"]


def _rows(ran: Ran) -> list[str]:
    shown = ran.rows[:ROWS_SHOWN]
    cells = [ran.columns] + [[_cell(v) for v in row] for row in shown]
    widths = [max(len(row[i]) for row in cells) for i in range(len(ran.columns))]
    count = f"{len(ran.rows)}{'+' if ran.truncated else ''}"
    out = [f"Rows ({count})"]
    out += [
        ("  " + "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True))).rstrip()
        for row in cells
    ]
    if len(ran.rows) > len(shown):
        out.append(f"  … {len(ran.rows) - len(shown)} more")
    if ran.truncated:
        out.append("  (the source returned only these; the query has more)")
    return out


def _cell(value: Any) -> str:
    return _clip("NULL" if value is None else str(value), CELL_WIDTH)


def _clip(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _indent(text: str) -> list[str]:
    return [f"  {line}" for line in text.strip().splitlines()]


# --- the command -------------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    engine: Sequence[str] = (sys.executable, "-m", "ontological.engine"),
) -> int:
    """`engine` is the command that runs the engine, without arguments."""
    ap = argparse.ArgumentParser(
        prog="ontological",
        description="Ontological from the command line. Other options go to the engine it starts.",
    )
    commands = ap.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="answer one question; uses Claude quota")
    ask.add_argument("question")
    ask.add_argument("--source", required=True, metavar="NAME", help="the database to answer from")
    ask.add_argument("--no-critic", action="store_true", help="skip the critic's review")
    ask.add_argument("--app-data", type=Path, default=None, help="<appData>/Ontological")
    known, engine_args = ap.parse_known_args(sys.argv[1:] if argv is None else argv)
    app_data = (known.app_data or engine_main.default_app_data()).absolute()
    start = [*engine, "--app-data", str(app_data), *engine_args, "--headless"]
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(name)s: %(message)s")

    try:
        asked = anyio.run(
            lambda: ask_engine(
                app_data, start, known.question, known.source, critic=not known.no_critic
            )
        )
    except AskFailed as e:
        print(f"ontological ask: {e}", file=sys.stderr)
        return 1
    print(render(asked))
    return 0 if asked.verified else 2


def cli() -> None:
    """The `ontological` console script."""
    sys.exit(main())
