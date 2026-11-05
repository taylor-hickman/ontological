"""The engine's SIGTERM/SIGINT handler (`_Stop`): it only records the signal, and the engine's
loop logs it, since logging inside a signal handler can hit a reentrant write to stderr."""

from __future__ import annotations

import logging
import signal

import pytest

from ontological.engine.main import _Stop  # pyright: ignore[reportPrivateUsage]


def test_the_handler_records_the_signal_without_logging(caplog: pytest.LogCaptureFixture) -> None:
    stop = _Stop()
    assert not stop.requested
    with caplog.at_level(logging.INFO, logger="ontological.engine"):
        stop(signal.SIGTERM, None)
        stop(signal.SIGINT, None)
    assert stop.requested
    assert stop.signum == signal.SIGTERM
    assert caplog.records == []


def test_the_loop_logs_the_first_signal_once(caplog: pytest.LogCaptureFixture) -> None:
    stop = _Stop()
    with caplog.at_level(logging.INFO, logger="ontological.engine"):
        stop.log_once()  # nothing asked to stop yet
        stop(signal.SIGINT, None)
        stop.log_once()
        stop.log_once()
    assert [r.getMessage() for r in caplog.records] == ["SIGINT: stopping"]
