"""The jobs an engine is running now (ARCHITECTURE.md §4.4): a job counts until it returns, a
paused one too, and a database runs one job of a kind at a time."""

from __future__ import annotations

import pytest

from ontological.engine.lifetime import Lifetime
from ontological.jobs.active import ActiveJobs, JobBusy


def test_a_running_job_keeps_the_engine_up_until_it_returns() -> None:
    now = [0.0]
    jobs = ActiveJobs()
    lifetime = Lifetime(idle_timeout_s=60, clock=lambda: now[0], jobs_running=jobs.count)

    with jobs.track("compile", "chinook"):
        now[0] += 3600
        assert jobs.count() == 1
        assert not lifetime.should_exit()

    assert jobs.count() == 0
    now[0] += 60
    assert lifetime.should_exit()


def test_a_database_runs_one_compile_at_a_time() -> None:
    jobs = ActiveJobs()

    with jobs.track("compile", "chinook"):
        with pytest.raises(JobBusy), jobs.track("compile", "chinook"):
            pass
        with jobs.track("compile", "wwi"):
            assert jobs.count() == 2

    with jobs.track("compile", "chinook"):
        assert jobs.count() == 1


def test_a_job_that_raises_stops_counting() -> None:
    jobs = ActiveJobs()

    with pytest.raises(RuntimeError), jobs.track("compile", "chinook"):
        raise RuntimeError

    assert jobs.count() == 0
