"""Leases and idle shutdown (ARCHITECTURE.md §4.4), on a fake clock.

A client renews its lease every `renew_every_s` (30 s); a lease that misses three renewals
expires. The engine may exit once it has had no live lease, no request in flight and no running
job for `idle_timeout_s` (10 minutes).
"""

from __future__ import annotations

import pytest

from ontological.engine.lifetime import Closing, Lifetime


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


@pytest.fixture
def clock() -> Clock:
    return Clock()


def lifetime(clock: Clock, *, jobs: list[int] | None = None) -> Lifetime:
    running = jobs if jobs is not None else [0]
    return Lifetime(
        idle_timeout_s=600, renew_every_s=30, clock=clock, jobs_running=lambda: running[0]
    )


def test_the_defaults_are_section_4_4s() -> None:
    lt = Lifetime()
    assert lt.idle_timeout_s == 600
    assert lt.renew_every_s == 30
    assert lt.expires_after_s == 90


def test_a_lease_expires_after_three_missed_renewals(clock: Clock) -> None:
    lt = lifetime(clock)
    lease = lt.take("electron")
    assert (lease.renew_every_s, lease.expires_after_s) == (30, 90)
    clock.advance(89.9)
    assert lt.live_leases() == 1
    clock.advance(0.2)
    assert lt.live_leases() == 0
    assert lt.renew(lease.id) is None, "an expired lease can't be renewed; take a new one"


def test_renewing_restarts_the_lease_clock(clock: Clock) -> None:
    lt = lifetime(clock)
    lease = lt.take()
    for _ in range(10):
        clock.advance(30)
        renewed = lt.renew(lease.id)
        assert renewed is not None and renewed.id == lease.id
    clock.advance(89)
    assert lt.live_leases() == 1


def test_releasing_a_lease_ends_it(clock: Clock) -> None:
    lt = lifetime(clock)
    a, b = lt.take(), lt.take()
    assert a.id != b.id
    assert lt.release(a.id)
    assert not lt.release(a.id)
    assert lt.renew(a.id) is None
    assert lt.live_leases() == 1


def test_lease_ids_are_unguessable(clock: Clock) -> None:
    lt = lifetime(clock)
    assert len(lt.take().id) >= 22


def test_an_idle_engine_exits_after_the_timeout_and_not_before(clock: Clock) -> None:
    lt = lifetime(clock)
    clock.advance(599)
    assert not lt.should_exit()
    clock.advance(1)
    assert lt.should_exit()


def test_a_renewed_lease_keeps_the_engine_up(clock: Clock) -> None:
    lt = lifetime(clock)
    lease = lt.take()
    for _ in range(100):  # 50 minutes
        clock.advance(30)
        assert lt.renew(lease.id) is not None
        assert not lt.should_exit()


def test_idle_time_counts_from_when_a_lapsed_lease_expired(clock: Clock) -> None:
    lt = lifetime(clock)
    lt.take()
    clock.advance(90 + 599)  # expired at +90, then idle for 599 s
    assert not lt.should_exit()
    clock.advance(1)
    assert lt.should_exit()


def test_idle_time_counts_from_the_last_release(clock: Clock) -> None:
    lt = lifetime(clock)
    lease = lt.take()
    for _ in range(100):
        clock.advance(30)
        assert lt.renew(lease.id) is not None
    clock.advance(10)
    assert lt.release(lease.id)
    clock.advance(599)
    assert not lt.should_exit()
    clock.advance(1)
    assert lt.should_exit()


def test_a_request_in_flight_keeps_the_engine_up(clock: Clock) -> None:
    lt = lifetime(clock)
    lt.request_started()
    clock.advance(5000)  # a long SSE stream, say
    assert not lt.should_exit()
    lt.request_finished()
    clock.advance(599)
    assert not lt.should_exit(), "idle time counts from the end of the last request"
    clock.advance(1)
    assert lt.should_exit()


def test_a_request_resets_the_idle_clock(clock: Clock) -> None:
    lt = lifetime(clock)
    clock.advance(500)
    lt.request_started()
    lt.request_finished()
    clock.advance(599)
    assert not lt.should_exit()
    clock.advance(1)
    assert lt.should_exit()


def test_a_running_job_keeps_the_engine_up(clock: Clock) -> None:
    jobs = [1]  # a paused job counts as running too: the job runner reports it
    lt = lifetime(clock, jobs=jobs)
    clock.advance(5000)
    assert not lt.should_exit()
    jobs[0] = 0
    clock.advance(599)
    assert not lt.should_exit()
    clock.advance(1)
    assert lt.should_exit()


def test_once_it_decides_to_exit_it_takes_no_new_leases(clock: Clock) -> None:
    lt = lifetime(clock)
    clock.advance(600)
    assert lt.should_exit()
    assert lt.closing
    with pytest.raises(Closing):
        lt.take()


def test_the_timeouts_are_configurable() -> None:
    lt = Lifetime(idle_timeout_s=1.5, renew_every_s=0.2)
    assert lt.idle_timeout_s == 1.5
    assert lt.expires_after_s == pytest.approx(0.6)
