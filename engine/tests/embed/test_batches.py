"""Bulk batches: sorted by length, capped by padded tokens (ARCHITECTURE.md §7.1)."""

from __future__ import annotations

import pytest

from ontological.embed.embedder import cap_from_timing, token_batches


def padded(batch: list[int], lengths: list[int]) -> int:
    return len(batch) * max(lengths[i] for i in batch)


def test_batches_hold_every_text_once_shortest_first_within_the_cap() -> None:
    lengths = [50, 3, 120, 7, 7, 64, 30, 2, 90, 11]
    batches = token_batches(lengths, cap_tokens=128)

    assert sorted(i for b in batches for i in b) == list(range(len(lengths)))
    flat = [lengths[i] for b in batches for i in b]
    assert flat == sorted(flat)
    assert all(padded(b, lengths) <= 128 for b in batches)


def test_a_batch_fills_up_to_the_cap() -> None:
    assert token_batches([10] * 8, cap_tokens=40) == [[0, 1, 2, 3], [4, 5, 6, 7]]


def test_a_text_longer_than_the_cap_goes_alone() -> None:
    assert token_batches([5, 512, 5], cap_tokens=100) == [[0, 2], [1]]


def test_no_texts_no_batches() -> None:
    assert token_batches([], cap_tokens=1024) == []


@pytest.mark.parametrize(
    ("tokens", "ms", "cap"),
    [
        (1024, 95.0, 1024),  # an M5 Max: 100 ms holds more than the ceiling
        (1024, 200.0, 512),  # half as fast: half the tokens
        (1024, 400.0, 256),
        (1024, 10_000.0, 64),  # absurdly slow: the floor
    ],
)
def test_the_cap_is_what_100_ms_holds_at_the_measured_rate(
    tokens: int, ms: float, cap: int
) -> None:
    assert cap_from_timing(tokens, ms) == cap
