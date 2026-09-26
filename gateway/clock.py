"""Time for the gateway.

Production uses the wall clock. The chaos benchmark uses a scaled clock so that
four minutes of simulated traffic run through the very same code in a few
seconds: the gateway reads simulated seconds, and every sleep or timeout it
waits on is shortened by the same factor.
"""
import asyncio
import time


class Clock:
    def now(self) -> float:
        return time.time()

    def to_real(self, seconds: float) -> float:
        return seconds

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ScaledClock(Clock):
    """One simulated second lasts `scale` real seconds."""

    def __init__(self, scale: float, start: float = 0.0):
        self.scale = scale
        self._start = start
        self._t0 = time.monotonic()

    def now(self) -> float:
        return self._start + (time.monotonic() - self._t0) / self.scale

    def to_real(self, seconds: float) -> float:
        return seconds * self.scale

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self.scale)
