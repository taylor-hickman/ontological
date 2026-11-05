"""Turn a pytest JUnit XML report into S6's pass/fail list.

    python3 report.py <junit.xml>

Prints one line per test file with its pass, fail and skip counts, then every test that did not
pass, with the first line of its message. A skip counts as a failure (docs/BUILD_PLAN.md S6),
so the exit status is 1 if anything failed, errored or skipped.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict


def outcome(case: ET.Element) -> tuple[str, str]:
    for tag in ("failure", "error", "skipped"):
        found = case.find(tag)
        if found is not None:
            message = (found.get("message") or found.text or "").strip().splitlines()
            return tag, message[0] if message else ""
    return "passed", ""


def main(path: str) -> int:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bad: list[tuple[str, str, str, str]] = []
    for case in ET.parse(path).iter("testcase"):
        module = case.get("classname", "").split(".")
        file = next((p for p in module if p.startswith("test_")), ".".join(module))
        result, message = outcome(case)
        counts[file][result] += 1
        if result != "passed":
            bad.append((file, case.get("name", ""), result, message))

    total: dict[str, int] = defaultdict(int)
    print("| File | Passed | Failed | Errors | Skipped |")
    print("|---|---:|---:|---:|---:|")
    for file in sorted(counts):
        c = counts[file]
        for k, v in c.items():
            total[k] += v
        print(f"| `{file}.py` | {c['passed']} | {c['failure']} | {c['error']} | {c['skipped']} |")
    print(
        f"| **all** | {total['passed']} | {total['failure']} | {total['error']} "
        f"| {total['skipped']} |"
    )
    if bad:
        print()
        for file, name, result, message in bad:
            print(f"- {result}: `{file}::{name}`: {message[:200]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
