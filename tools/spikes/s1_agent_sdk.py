#!/usr/bin/env python3
"""Spike S1: does the Agent SDK do what the gated agent needs?

Runs the Agent SDK contract tests (engine/tests/contract/test_agent_sdk.py) twice:

- plain:   the environment of a fresh login shell (`env -i` + `$SHELL -l`), as when the
           engine is started from a terminal or by Electron.
- current: this process's environment. Run the script from inside a Claude Code session
           (for example `! python3 tools/spikes/s1_agent_sdk.py` in Claude Code) to get the
           nested case, where the engine inherits CLAUDECODE, CLAUDE_CODE_ENTRYPOINT and the
           other session variables as an engine started by the MCP shim would.

It prints pass or fail for every behaviour in each environment, the session variables each
run had, and every difference between the two runs' init messages and tool logs.

Each run spends a little quota: about 5 `claude` spawns on the contract model (default haiku,
override with ONTOLOGICAL_CONTRACT_MODEL).

Usage: python3 tools/spikes/s1_agent_sdk.py [--only plain|current]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
TESTS = "tests/contract/test_agent_sdk.py"
SESSION_VAR_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PID", "CLAUDE_EFFORT")
# Expected to differ between any two runs.
VOLATILE = {"init_session_id"}


def plain_env() -> dict[str, str]:
    shell = os.environ.get("SHELL", "/bin/zsh")
    seed = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "TERM") if k in os.environ}
    seed["SHELL"] = shell
    out = subprocess.run(
        ["/usr/bin/env", "-i", *(f"{k}={v}" for k, v in seed.items()), shell, "-l", "-c", "env -0"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    env = dict(line.split("=", 1) for line in out.split("\0") if "=" in line)
    for key in ("ONTOLOGICAL_CONTRACT_MODEL",):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def run_contract(label: str, env: dict[str, str], workdir: Path) -> dict[str, Any]:
    report = workdir / f"{label}.json"
    junit = workdir / f"{label}.xml"
    env = {**env, "ONTOLOGICAL_CONTRACT": "1", "S1_REPORT": str(report)}
    env.pop("VIRTUAL_ENV", None)
    cmd = ["uv", "run", "--directory", str(ENGINE), "pytest", TESTS, "-q", f"--junitxml={junit}"]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    outcomes: dict[str, str] = {}
    if junit.exists():
        for case in ET.parse(junit).getroot().iter("testcase"):
            name = case.get("name", "?").removeprefix("test_")
            failed = case.find("failure") is not None or case.find("error") is not None
            skipped = case.find("skipped") is not None
            outcomes[name] = "FAIL" if failed else "skip" if skipped else "pass"
    observed = json.loads(report.read_text()) if report.exists() else {}
    return {"outcomes": outcomes, "observed": observed, "exit": proc.returncode, "log": proc.stdout}


def diff(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for key in sorted(set(a) | set(b)):
            if key in VOLATILE:
                continue
            out += diff(a.get(key), b.get(key), f"{path}.{key}" if path else key)
        return out
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def comparable(observed: dict[str, Any]) -> dict[str, Any]:
    """The parts of a run that should not depend on the environment."""
    out: dict[str, Any] = {}
    for name, run in observed.items():
        if not isinstance(run, dict) or "init" not in run:
            continue
        result = run.get("result") or {}
        out[name] = {
            "init": run["init"],
            "system_subtypes": run["system_subtypes"],
            "hook_events": run["hook_events"],
            "assistant_models": run["assistant_models"],
            "tool_log": [(c[0], c[2]) for c in run["tool_log"]],
            "result": {k: result.get(k) for k in ("subtype", "is_error")},
            "denied": [d["tool_name"] for d in result.get("permission_denials") or []],
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=("plain", "current"))
    args = parser.parse_args()

    envs = {"plain": plain_env, "current": lambda: dict(os.environ)}
    labels = [args.only] if args.only else list(envs)
    workdir = Path(tempfile.mkdtemp(prefix="s1-"))
    runs = {label: run_contract(label, envs[label](), workdir) for label in labels}

    names = sorted({n for r in runs.values() for n in r["outcomes"]})
    width = max(map(len, names), default=10)
    print(f"{'behaviour':<{width}}  " + "  ".join(f"{label:>7}" for label in labels))
    for name in names:
        cells = "  ".join(f"{runs[label]['outcomes'].get(name, '-'):>7}" for label in labels)
        print(f"{name:<{width}}  {cells}")

    for label in labels:
        observed = runs[label]["observed"]
        gated = observed.get("gated", {})
        present = ", ".join(observed.get("session_vars", [])) or "none"
        print(f"\n[{label}] session variables: {present}")
        print(f"[{label}] resolved model: {gated.get('init', {}).get('model')}")
        usage = (gated.get("result") or {}).get("model_usage") or {}
        for model, entry in usage.items():
            print(f"[{label}] usage {model}: in={entry['inputTokens']} out={entry['outputTokens']}")
        for raw in gated.get("rate_limits", [])[:1]:
            print(f"[{label}] rate limit: {json.dumps(raw)}")
        if runs[label]["exit"] not in (0, 1):
            print(runs[label]["log"])

    if len(labels) == 2:
        delta = diff(comparable(runs["plain"]["observed"]), comparable(runs["current"]["observed"]))
        print("\nplain vs current:", "no differences" if not delta else "")
        for line in delta:
            print(f"  {line}")

    print(f"\nreports: {workdir}")
    failed = any("FAIL" in r["outcomes"].values() or not r["outcomes"] for r in runs.values())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
