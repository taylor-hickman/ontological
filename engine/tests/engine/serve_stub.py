"""The real engine process with a fake store, for the engine process tests.

    serve_stub.py <the engine's arguments>

It runs `ontological.engine.main.main` with the same arguments, but in place of the bundled
Postgres the store appends what it was asked to do to <appData>/store.log: `start` when the
engine starts it, `quit-dump` and `stop` when the engine stops it. If <appData>/slow-start exists,
the store takes that many seconds to start; if <appData>/slow-stop exists, it takes that many
between `quit-dump` and `stop`. Jobs come from <appData>/jobs.json (`fake_jobs.py`), the ontology
from <appData>/ontology.json (`fake_ontology.py`) and answers from `fake_answers.py`. Its sources
live in memory, starting with those in <appData>/sources.json if it exists (`fake_sources.py`), and
the test call answers without spawning `claude`.
`--headless` relaunches this file, so the detached engine has the fake store too.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from fake_answers import FakeAnswers
from fake_jobs import FakeJobs
from fake_ontology import FakeOntology
from fake_sources import FakeClaude, FakeSources

from ontological.embed.embedder import LocalEmbedder
from ontological.engine import main
from ontological.jobs.active import ActiveJobs
from ontological.jobs.service import JobsService
from ontological.jobs.staleness import StalenessCheck


class FakeStore:
    def __init__(self, app_data: Path) -> None:
        self.app_data = app_data
        self.log = app_data / "store.log"
        self.slow_start = app_data / "slow-start"
        self.slow_stop = app_data / "slow-stop"

    def _write(self, *lines: str) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "a") as f:
            f.writelines(f"{line}\n" for line in lines)

    def start(self) -> None:
        if self.slow_start.exists():
            time.sleep(float(self.slow_start.read_text()))
        self._write("start")

    def stop(self) -> None:
        self._write("quit-dump")
        if self.slow_stop.exists():
            time.sleep(float(self.slow_stop.read_text()))
        self._write("stop")

    def staleness_check(self) -> StalenessCheck:
        return StalenessCheck(find=lambda: [])  # no catalog, so no sources to refresh

    def sources(self, jobs: ActiveJobs, embedder: LocalEmbedder | None) -> FakeSources:
        return FakeSources.seeded(self.app_data / "sources.json")

    def claude(self, config: main.Config) -> FakeClaude:
        return FakeClaude(config.claude_cli)

    def jobs(self, active: ActiveJobs, embedder: LocalEmbedder | None) -> JobsService:
        return FakeJobs(self.app_data)

    def ontology(self, embedder: LocalEmbedder | None) -> FakeOntology:
        return FakeOntology(self.app_data)

    def answers(self, embedder: LocalEmbedder | None) -> FakeAnswers:
        return FakeAnswers()


if __name__ == "__main__":
    sys.exit(
        main.main(
            sys.argv[1:],
            store=lambda config: FakeStore(config.app_data),
            relaunch=[sys.executable, __file__],
        )
    )
