from __future__ import annotations

import asyncio

import pytest

from app.ari import dtmf


async def test_collects_fixed_length() -> None:
    collector = dtmf.DigitCollector(
        length=4, digit_timeout=1.0, total_timeout=5.0
    )
    for digit in "1234":
        collector.feed(digit)
    result = await collector.collect()
    assert result.digits == "1234"
    assert result.submitted
    assert not result.timed_out


async def test_hash_submits_early() -> None:
    collector = dtmf.DigitCollector(
        length=4, digit_timeout=1.0, total_timeout=5.0
    )
    for digit in "12#":
        collector.feed(digit)
    result = await collector.collect()
    assert result.digits == "12"
    assert result.submitted


async def test_star_clears_the_buffer() -> None:
    collector = dtmf.DigitCollector(
        length=4, digit_timeout=1.0, total_timeout=5.0
    )
    for digit in "99*1234":
        collector.feed(digit)
    result = await collector.collect()
    assert result.digits == "1234"


async def test_inter_digit_timeout() -> None:
    collector = dtmf.DigitCollector(
        length=4, digit_timeout=0.05, total_timeout=5.0
    )
    collector.feed("1")
    result = await collector.collect()
    assert result.digits == "1"
    assert result.timed_out
    assert not result.submitted


async def test_total_timeout_wins_over_slow_typing() -> None:
    collector = dtmf.DigitCollector(
        length=4, digit_timeout=0.5, total_timeout=0.12
    )

    async def slow_typist() -> None:
        for digit in "1234":
            await asyncio.sleep(0.05)
            collector.feed(digit)

    task = asyncio.create_task(slow_typist())
    result = await collector.collect()
    task.cancel()
    assert result.timed_out
    assert len(result.digits) < 4


async def test_reset_drops_pending_digits() -> None:
    collector = dtmf.DigitCollector(
        length=2, digit_timeout=0.05, total_timeout=1.0
    )
    collector.feed("9")
    collector.reset()
    collector.feed("1")
    collector.feed("2")
    result = await collector.collect()
    assert result.digits == "12"


@pytest.mark.parametrize("junk", ["A", "B", "C", "D"])
async def test_non_numeric_digits_are_ignored(junk: str) -> None:
    collector = dtmf.DigitCollector(
        length=2, digit_timeout=0.5, total_timeout=2.0
    )
    for digit in (junk, "7", "8"):
        collector.feed(digit)
    result = await collector.collect()
    assert result.digits == "78"
