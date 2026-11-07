"""The run report (BUILD_PLAN.md, Scoring and runs), from a run's run.json alone.

It shows what that section records per run: execution accuracy, the share of answers the gate
verified, spawns and turns per question, per-model tokens (the answering model's and Claude
Code's own side calls apart), the change in utilization, and the resolved model IDs. Beside
them: the control arm on the same questions with the questions each arm alone got right, why
each miss missed, and the dialect artifacts: answers that match only under the verifier's
tolerance rule.

As in ontology-sql-eval's `compare.py`, a run in which more than `HEALTH_THRESHOLD` of an arm's
questions got no answer at all (a rate limit, an error) isn't reported as a score: it would read
as a weak model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, cast

from . import runs

HEALTH_THRESHOLD = 0.05
NOISE_POINTS = 10  # with ~80 questions only differences of about this much stand out

Record = runs.Record


def hits(records: Iterable[Record]) -> tuple[int, int]:
    rs = list(records)
    return sum(1 for r in rs if r["score"]["match"]), len(rs)


def pct(k: int, n: int) -> str:
    return f"{100 * k / n:.1f}% ({k}/{n})" if n else "n/a"


def no_answer_share(records: Sequence[Record]) -> float:
    if not records:
        return 0.0
    return sum(1 for r in records if r["score"]["failure"] == "no_answer") / len(records)


def healthy(records: Sequence[Record]) -> bool:
    return no_answer_share(records) <= HEALTH_THRESHOLD


def all_calls(records: Iterable[Record]) -> list[runs.Call]:
    return [c for r in records for c in r["calls"]]


def models_seen(records: Iterable[Record]) -> dict[str, dict[str, Counter[str]]]:
    """Per task: the route models, the models they resolved to and the models that answered."""
    out: dict[str, dict[str, Counter[str]]] = {}
    for c in all_calls(records):
        seen = out.setdefault(
            c["task"], {"route": Counter(), "resolved": Counter(), "answered": Counter()}
        )
        seen["route"][c["model"]] += 1
        if c.get("resolved_model"):
            seen["resolved"][c["resolved_model"]] += 1
        if c.get("answered_model"):
            seen["answered"][c["answered_model"]] += 1
    return out


def unpinned(run: Mapping[str, Any]) -> list[str]:
    """Calls whose route model isn't the config's pinned one: the engine ran other routes."""
    pinned = {t: r["model"] for t, r in run["routes"].items()}
    out: list[str] = []
    for task, seen in models_seen(run["agent"]).items():
        for model in seen["route"]:
            if pinned.get(task) != model:
                out.append(f"{task} ran on route model {model}, not {pinned.get(task)}")
    return out


def control_records(run: Mapping[str, Any]) -> list[Record]:
    arm = cast(dict[str, Any] | None, run.get("control"))
    return cast(list[Record], arm["records"]) if arm else []


def summarize(run: Mapping[str, Any]) -> dict[str, Any]:
    """The report's numbers, for code that compares runs."""
    agent: list[Record] = run["agent"]
    control = control_records(run)
    both = {r["key"] for r in control} & {r["key"] for r in agent}
    calls = all_calls(agent)
    n = len(agent)
    util = run.get("utilization", {})
    return {
        "questions": n,
        "execution_accuracy": hits(agent),
        "control_accuracy": hits(r for r in control if r["key"] in both),
        "agent_on_control_questions": hits(r for r in agent if r["key"] in both),
        "verified": sum(1 for r in agent if r["verified"]),
        "spawns_per_question": runs.spawns(calls) / n if n else 0.0,
        "turns_per_question": runs.turns(calls) / n if n else 0.0,
        "tokens": runs.tokens(calls),
        "control_tokens": runs.tokens(all_calls(control)),
        "utilization_change": _changes(util),
        "healthy": healthy(agent) and healthy(control),
    }


def _changes(util: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for arm, (a, b) in {
        "agent": ("start", "after_agent"),
        "control": ("after_agent", "after_control"),
    }.items():
        if a in util and b in util:
            out[arm] = runs.utilization_change(util[a]["windows"], util[b]["windows"])
    return out


# --- Markdown ------------------------------------------------------------------------------------


def markdown(run: Mapping[str, Any]) -> str:
    agent: list[Record] = run["agent"]
    control = control_records(run)
    s = summarize(run)
    out: list[str] = [f"# Eval run {run['run_id']}", ""]
    out += _header(run, agent)
    if not agent:
        return "\n".join([*out, "", "No question was answered."]) + "\n"

    out += ["", "## Execution accuracy", ""]
    for arm, records in (("agent", agent), ("control", control)):
        if records and not healthy(records):
            out.append(
                f"**Not a score for the {arm}:** {no_answer_share(records):.0%} of its questions "
                f"got no answer (a rate limit or an error), above {HEALTH_THRESHOLD:.0%}."
            )
    out += _accuracy(agent, control, s)
    out += ["", "## The gate", ""]
    v_hits = hits(r for r in agent if r["verified"])
    u_hits = hits(r for r in agent if not r["verified"])
    out.append(f"- Verified by the post-run check: {pct(s['verified'], len(agent))}")
    out.append(f"- Execution accuracy of verified answers: {pct(*v_hits)}")
    out.append(f"- Execution accuracy of unverified answers: {pct(*u_hits)}")
    reasons = Counter(_first_words(r["reason"]) for r in agent if not r["verified"])
    out += [f"  - unverified: {why} ({k})" for why, k in reasons.most_common()]

    out += ["", "## Why answers missed", ""]
    out += _failures("Agent", agent)
    if control:
        out += _failures("Control", control)
    artifacts = [r for r in agent if r["score"].get("dialect_artifact")]
    out += ["", f"## Dialect artifacts ({len(artifacts)})", ""]
    out.append(
        "Answers whose rows differ from gold only in type or float precision: they match under "
        "the verifier's tolerance rule (M1.10). A run scored before the rule kept them as misses."
    )
    for r in artifacts:
        sc = r["score"]
        out.append(f"- {r['key']} ({r['db']}): {_verdict(r)}, gold {sc['gold_sample'][:2]}, "
                   f"agent {sc['pred_sample'][:2]}")  # fmt: skip

    out += ["", "## Usage", ""]
    out += _usage(agent, control, s)
    out += ["", "## Models", ""]
    out += _models(run, agent, control)
    out += ["", "## Questions", ""]
    out += _questions(agent, {r["key"]: r for r in control})
    return "\n".join(out) + "\n"


def _header(run: Mapping[str, Any], agent: Sequence[Record]) -> list[str]:
    routes = ", ".join(f"{t} {r['model']} ({r['effort']})" for t, r in run["routes"].items())
    out = [
        f"- Set: {run['set']}, {len(agent)} of {run['questions_in_set']} questions answered"
        + (f" ({len(run['questions'])} selected)" if len(run["questions"]) != len(agent) else ""),
        f"- Config: {run['config']}, critic {'on' if run['critic'] else 'off'}",
        f"- Routes: {routes}",
        f"- Started {run['started_at']}, finished {run.get('finished_at') or 'not yet'}",
    ]
    if run.get("stopped"):
        out.append(f"- **Stopped:** {run['stopped']}")
    if run.get("error"):
        out.append(f"- **Failed:** {run['error']}")
    if other := Counter(t for r in agent for t in r.get("other_calls", [])):
        out.append(
            "- **Warning:** the engine made calls that aren't answering's while the run asked: "
            + ", ".join(f"{t} ({k})" for t, k in other.items())
        )
    return out


def _accuracy(
    agent: Sequence[Record], control: Sequence[Record], s: Mapping[str, Any]
) -> list[str]:
    ctrl = {r["key"]: r for r in control}
    out = ["| | Agent | Control |", "|---|---|---|"]

    def row(label: str, keep: Callable[[Record], bool]) -> None:
        a = [r for r in agent if keep(r)]
        if not a:
            return
        c = [ctrl[r["key"]] for r in a if r["key"] in ctrl]
        out.append(f"| {label} | {pct(*hits(a))} | {pct(*hits(c)) if c else '-'} |")

    row("All", lambda r: True)
    for source, label in (("wwi", "WWI"), ("bird", "BIRD")):
        row(label, lambda r, src=source: r["source"] == src)
    for db in sorted({r["db"] for r in agent if r["source"] == "bird"}):
        row(f"BIRD {db}", lambda r, d=db: r["db"] == d)
    for level in ("simple", "moderate", "challenging"):
        row(f"BIRD {level}", lambda r, lv=level: r["source"] == "bird" and r["difficulty"] == lv)

    if control:
        a_k, n = s["agent_on_control_questions"]
        c_k, _ = s["control_accuracy"]
        if n:
            delta = 100 * (a_k - c_k) / n
            note = "" if abs(delta) >= NOISE_POINTS else f", within the ~{NOISE_POINTS}-point noise"
            out += ["", f"The agent minus the control on the same {n} questions: "
                        f"{delta:+.1f} points{note}."]  # fmt: skip
        only_agent = [r["key"] for r in agent if r["score"]["match"]
                      and r["key"] in ctrl and not ctrl[r["key"]]["score"]["match"]]  # fmt: skip
        only_control = [r["key"] for r in agent if not r["score"]["match"]
                        and r["key"] in ctrl and ctrl[r["key"]]["score"]["match"]]  # fmt: skip
        out.append(f"- Only the agent got right: {', '.join(only_agent) or 'none'}")
        out.append(f"- Only the control got right: {', '.join(only_control) or 'none'}")
    return out


def _failures(label: str, records: Sequence[Record]) -> list[str]:
    counts = Counter(r["score"]["failure"] for r in records if not r["score"]["match"])
    if not counts:
        return [f"- {label}: no misses"]
    return [f"- {label}: " + ", ".join(f"{f} ({k})" for f, k in counts.most_common())]


def _usage(agent: Sequence[Record], control: Sequence[Record], s: Mapping[str, Any]) -> list[str]:
    calls = all_calls(agent)
    per_q = [runs.spawns(r["calls"]) for r in agent]
    turns_q = [runs.turns(r["calls"]) for r in agent]
    secs = [float(r["seconds"]) for r in agent]
    out = [
        f"- Agent: {s['spawns_per_question']:.2f} spawns and {s['turns_per_question']:.1f} turns "
        f"per question (at most {max(per_q)} and {max(turns_q)}), "
        f"{sum(secs) / len(secs):.0f}s per question, {runs.spawns(calls)} spawns in all",
    ]
    if control:
        c_calls = all_calls(control)
        out.append(
            f"- Control: {runs.spawns(c_calls) / len(control):.2f} spawns per question, "
            f"{runs.spawns(c_calls)} in all"
        )
    out += ["", "Tokens, by the model that answered and Claude Code's own side calls:", ""]
    out += [
        "| Arm | Model | Role | Input | Output | Cache read | Cache write | API-equivalent |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for arm, tokens in (("agent", s["tokens"]), ("control", s["control_tokens"])):
        for role in ("answering", "side"):
            for model, t in sorted(tokens[role].items()):
                out.append(
                    f"| {arm} | {model} | {'side calls' if role == 'side' else 'answering'} | "
                    f"{t['input']:,.0f} | {t['output']:,.0f} | {t['cache_read']:,.0f} | "
                    f"{t['cache_write']:,.0f} | ${t['cost_usd']:.3f} |"
                )
    out += ["", "Utilization change per window (the runner's pings before and after each arm):"]
    out.append("")
    changes: Mapping[str, Mapping[str, float]] = s["utilization_change"]
    if not changes:
        out.append("- not measured")
    for arm, windows in changes.items():
        text = ", ".join(f"{w} {d:+.4g}" for w, d in windows.items()) or "no window reported"
        out.append(f"- {arm}: {text}")
    return out


def _models(
    run: Mapping[str, Any], agent: Sequence[Record], control: Sequence[Record]
) -> list[str]:
    out = ["| Arm | Task | Pinned | Resolved | Answered |", "|---|---|---|---|---|"]
    pinned = {t: r["model"] for t, r in run["routes"].items()}
    for arm, records in (("agent", agent), ("control", control)):
        for task, seen in sorted(models_seen(records).items()):
            out.append(
                f"| {arm} | {task} | {pinned.get(task, '-')} | {_counts(seen['resolved'])} | "
                f"{_counts(seen['answered'])} |"
            )
    fallbacks = sum(1 for c in all_calls(agent) + all_calls(control) if c.get("fallback"))
    if fallbacks:
        out.append(f"\n**{fallbacks} call(s) answered on the fallback model.**")
    for problem in unpinned(run):
        out.append(f"\n**Not pinned:** {problem}")
    return out


def _counts(c: Counter[str]) -> str:
    return ", ".join(f"{m} ({k})" for m, k in c.most_common()) or "-"


def _questions(agent: Sequence[Record], control: Mapping[str, Record]) -> list[str]:
    out = [
        "| Question | Database | Agent | Gate | Spawns | Turns | Control |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in agent:
        c = control.get(r["key"])
        out.append(
            f"| {r['key']} | {r['db']} | {_verdict(r)} | "
            f"{'verified' if r['verified'] else 'unverified'} | {runs.spawns(r['calls'])} | "
            f"{runs.turns(r['calls'])} | {_verdict(c) if c else '-'} |"
        )
    return out


def _verdict(r: Record) -> str:
    s = r["score"]
    return "match" if s["match"] else str(s["failure"])


def _first_words(reason: str | None, n: int = 8) -> str:
    words = (reason or "no reason given").split()
    return " ".join(words[:n]) + ("…" if len(words) > n else "")
