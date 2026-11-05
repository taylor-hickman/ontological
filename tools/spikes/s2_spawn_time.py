# /// script
# requires-python = "==3.12.*"
# dependencies = ["claude-agent-sdk>=0.2.178"]
# ///
"""Spike S2: how long does a spawn take?

Times `query()` for a one-line structured call, N times each on haiku, sonnet
and opus. Every call is a fresh `query()`, so a fresh `claude` process (cold:
no warm client), and the calls run back to back with no pause or concurrency.
Options follow ARCHITECTURE.md §6.2 for a structured call.

Per call it records:
  wall   time from calling query() to the end of its message stream
  spawn  time from calling query() to the CLI's `init` system message: the SDK's
         `claude -v` check, process spawn, CLI startup and the initialize
         handshake.
  close  time from the ResultMessage to the end of the stream: the SDK closing
         stdin and waiting for the process to exit.
  overhead  spawn + close, everything a call costs beyond the model's turn.
         S2's rule (p50 above 1.5 s: add a warm pool) is applied to this.
  cli    ResultMessage.duration_ms, the turn as the CLI clocks it
  api    ResultMessage.duration_api_ms. It counts every API request the CLI
         makes, some of them alongside startup, so wall - api is not a spawn time.

Run from the repo root:
  uv run tools/spikes/s2_spawn_time.py [--n 20] [--out results.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SystemMessage, query
from claude_agent_sdk import __version__ as sdk_version

MODELS = ("haiku", "sonnet", "opus")
PROMPT = "Classify the column `customer_email` as one of: identifier, contact, measure, date."
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["identifier", "contact", "measure", "date"]},
    },
    "required": ["kind"],
    "additionalProperties": False,
}
# The decision rule in BUILD_PLAN.md, S2.
WARM_POOL_THRESHOLD_S = 1.5
COLUMNS = ("wall", "spawn", "close", "overhead", "cli", "api")

# §6.2 strips these always, plus the session variables a Claude Code parent
# sets. S1 settles the exact list; here every CLAUDE* variable goes, so a run
# started from inside a Claude Code session behaves like one from a shell.
ALWAYS_STRIP = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
}


def sanitize_environ() -> list[str]:
    dropped = sorted(k for k in os.environ if k in ALWAYS_STRIP or k.startswith("CLAUDE"))
    for k in dropped:
        del os.environ[k]
    return dropped


@dataclass
class Call:
    model: str
    index: int
    wall_s: float
    spawn_s: float | None
    close_s: float | None
    overhead_s: float | None
    cli_s: float | None
    api_s: float | None
    resolved_model: str | None
    structured_output: Any
    ok: bool
    error: str | None


async def one_call(model: str, index: int, cli_path: str, cwd: str) -> Call:
    options = ClaudeAgentOptions(
        cli_path=cli_path,
        cwd=cwd,
        setting_sources=[],
        tools=[],
        allowed_tools=[],
        permission_mode="dontAsk",
        output_format={"type": "json_schema", "schema": SCHEMA},
        model=model,
        effort="low",
        max_turns=2,
        extra_args={"no-session-persistence": None},
    )
    spawn_s: float | None = None
    resolved: str | None = None
    result: ResultMessage | None = None
    result_at: float | None = None
    error: str | None = None
    start = time.perf_counter()
    try:
        async for msg in query(prompt=PROMPT, options=options):
            if isinstance(msg, SystemMessage) and msg.subtype == "init" and spawn_s is None:
                spawn_s = time.perf_counter() - start
                resolved = msg.data.get("model")
            elif isinstance(msg, ResultMessage):
                result = msg
                result_at = time.perf_counter() - start
    except Exception as e:  # noqa: BLE001 -- a spike reports failures rather than stopping
        error = f"{type(e).__name__}: {e}"
    wall_s = time.perf_counter() - start

    close_s = wall_s - result_at if result_at is not None else None
    api_s = result.duration_api_ms / 1000 if result else None
    output = result.structured_output if result else None
    ok = (
        error is None
        and result is not None
        and not result.is_error
        and isinstance(output, dict)
        and output.get("kind") in SCHEMA["properties"]["kind"]["enum"]
    )
    if result is not None and result.is_error and error is None:
        error = f"{result.subtype}: {result.errors or result.result}"
    return Call(
        model=model,
        index=index,
        wall_s=wall_s,
        spawn_s=spawn_s,
        close_s=close_s,
        overhead_s=spawn_s + close_s if spawn_s is not None and close_s is not None else None,
        cli_s=result.duration_ms / 1000 if result else None,
        api_s=api_s,
        resolved_model=resolved,
        structured_output=output,
        ok=ok,
        error=error,
    )


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile, so p95 of 20 values is the 19th."""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))
    return ordered[int(rank) - 1]


def summarize(calls: list[Call]) -> dict[str, dict[str, float | int | str | None]]:
    out: dict[str, dict[str, float | int | str | None]] = {}
    for model in [*MODELS, "all"]:
        rows = [c for c in calls if c.ok and (model == "all" or c.model == model)]
        if not rows:
            continue
        s: dict[str, float | int | str | None] = {
            "ok": len(rows),
            "failed": sum(1 for c in calls if not c.ok and (model == "all" or c.model == model)),
        }
        if model != "all":
            s["resolved_model"] = rows[0].resolved_model
            s["first_call_wall_s"] = rows[0].wall_s if rows[0].index == 0 else None
        for name in ("wall_s", "spawn_s", "close_s", "overhead_s", "cli_s", "api_s"):
            vals = [v for c in rows if (v := getattr(c, name)) is not None]
            if vals:
                s[f"{name}_p50"] = statistics.median(vals)
                s[f"{name}_p95"] = pct(vals, 95)
        out[model] = s
    return out


def fmt(v: float | str | None) -> str:
    return f"{v:.2f}" if isinstance(v, float) else ("-" if v is None else str(v))


async def run_calls(models: list[str], n: int, cli_path: str) -> list[Call]:
    calls: list[Call] = []
    with tempfile.TemporaryDirectory(prefix="s2-sandbox-") as cwd:
        for model in models:
            for i in range(n):
                c = await one_call(model, i, cli_path, cwd)
                calls.append(c)
                print(
                    f"{model:6} #{i + 1:2}  wall {c.wall_s:5.2f}s  spawn {fmt(c.spawn_s):>5}s  "
                    f"close {fmt(c.close_s):>5}s  cli {fmt(c.cli_s):>5}s  api {fmt(c.api_s):>5}s  "
                    f"{c.structured_output if c.ok else 'FAILED ' + str(c.error)}",
                    flush=True,
                )
    return calls


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--n", type=int, default=20, help="calls per model (default 20)")
    ap.add_argument("--models", nargs="+", default=list(MODELS))
    ap.add_argument("--out", help="write every call and the summary as JSON here")
    args = ap.parse_args()

    dropped = sanitize_environ()
    cli_path = shutil.which("claude")
    if cli_path is None:
        print("claude not found on PATH", file=sys.stderr)
        return 1
    cli_version = subprocess.run(
        [cli_path, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    print(f"claude {cli_version} at {cli_path}; claude-agent-sdk {sdk_version}")
    print(f"stripped from env: {', '.join(dropped) or 'nothing'}\n")

    calls = asyncio.run(run_calls(args.models, args.n, cli_path))

    summary = summarize(calls)
    print("\n" + " " * 16 + "".join(f"{h + ' p50/p95':<18}" for h in COLUMNS))
    for model, s in summary.items():
        print(
            f"{model:6} ({fmt(s['ok'])} ok)  "
            + "  ".join(
                f"{fmt(s.get(f'{k}_p50')):>5} / {fmt(s.get(f'{k}_p95')):<8}"
                for k in (f"{h}_s" for h in COLUMNS)
            )
        )

    overhead_p50 = summary.get("all", {}).get("overhead_s_p50")
    if isinstance(overhead_p50, float):
        verdict = (
            "add a warm pool of pre-spawned clients (§6.4) in M1"
            if overhead_p50 > WARM_POOL_THRESHOLD_S
            else "no warm pool"
        )
        print(f"\noverhead p50 {overhead_p50:.2f}s vs {WARM_POOL_THRESHOLD_S}s: {verdict}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(
                {
                    "date": time.strftime("%Y-%m-%d"),
                    "machine": f"{platform.system()} {platform.machine()} {platform.mac_ver()[0]}",
                    "claude": cli_version,
                    "claude_agent_sdk": sdk_version,
                    "prompt": PROMPT,
                    "calls": [asdict(c) for c in calls],
                    "summary": summary,
                },
                f,
                indent=2,
            )
    return 0 if all(c.ok for c in calls) else 2


if __name__ == "__main__":
    sys.exit(main())
