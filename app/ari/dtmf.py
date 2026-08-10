"""DTMF digit collection.

ARI has no ``getDigits`` primitive, only a stream of ``ChannelDtmfReceived``
events, so accumulation, the inter-digit timeout and the overall timeout are
implemented here.

Rules: ``#`` submits early, ``*`` clears the buffer, and the collector returns
as soon as the requested number of digits is in.
"""

from __future__ import annotations

import asyncio
import dataclasses


@dataclasses.dataclass(slots=True)
class CollectResult:
    """The outcome of one digit-collection attempt.

    Attributes:
        digits: The digits gathered so far, possibly fewer than requested.
        timed_out: Whether collection ended on a timeout.
        submitted: Whether the caller finished the input deliberately, either
            by reaching the requested length or by pressing ``#``.
    """

    digits: str
    timed_out: bool
    submitted: bool


class DigitCollector:
    """Collects DTMF digits for one channel.

    Digits arrive from the event loop through :meth:`feed`, while the consumer
    awaits :meth:`collect`.
    """

    def __init__(
        self, *, length: int, digit_timeout: float, total_timeout: float
    ) -> None:
        """Initializes the collector.

        Args:
            length: Number of digits that completes the input.
            digit_timeout: Seconds to wait between two digits.
            total_timeout: Seconds to wait for the whole input.
        """
        self.length = length
        self.digit_timeout = digit_timeout
        self.total_timeout = total_timeout
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def feed(self, digit: str) -> None:
        """Hands one received digit to the collector."""
        self._queue.put_nowait(digit)

    def reset(self) -> None:
        """Drops digits queued before the next attempt starts."""
        while not self._queue.empty():
            self._queue.get_nowait()

    async def collect(self) -> CollectResult:
        """Waits for the caller to type a code.

        Returns:
            The collected digits together with how the attempt ended. Digits
            that are neither numeric nor a control key are ignored.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.total_timeout
        buffer: list[str] = []

        while True:
            remaining_total = deadline - loop.time()
            if remaining_total <= 0:
                return CollectResult(
                    "".join(buffer), timed_out=True, submitted=False
                )
            wait_for = min(self.digit_timeout, remaining_total)
            try:
                digit = await asyncio.wait_for(
                    self._queue.get(), timeout=wait_for
                )
            except TimeoutError:
                return CollectResult(
                    "".join(buffer), timed_out=True, submitted=False
                )

            if digit == "#":
                return CollectResult(
                    "".join(buffer), timed_out=False, submitted=True
                )
            if digit == "*":
                buffer.clear()
                continue
            if not digit.isdigit():
                continue

            buffer.append(digit)
            if len(buffer) >= self.length:
                return CollectResult(
                    "".join(buffer), timed_out=False, submitted=True
                )
