"""Shared token bucket with a global pause, so one 429 slows every concurrent caller."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

_EPSILON = 1e-6


class TokenBucket:
    """Async token bucket.

    ``acquire()`` waits until a token is available. ``pause(seconds)`` blocks *everyone* until the
    pause ends; afterwards callers trickle back at the base rate (the bucket restarts empty) so a
    burst does not immediately re-trigger the limit.
    """

    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self.base_rate = float(rate)
        self.rate = float(rate)  # adapts: halves after a 429, recovers slowly on success
        self.min_rate = 0.2
        self.burst = int(burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._last = clock()
        self._paused_until = 0.0
        self._resume_pending = False
        self._last_slowdown = float("-inf")
        self._lock = asyncio.Lock()
        self.waited_s = 0.0

    def _refill(self, now: float) -> None:
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def pause(self, seconds: float) -> None:
        """Block all callers for ``seconds`` (extends, never shortens, an existing pause)."""
        until = self._clock() + max(0.0, seconds)
        if until > self._paused_until:
            self._paused_until = until
        self._tokens = 0.0
        self._resume_pending = True

    def slow_down(self) -> None:
        """Multiplicative decrease. At most once per second, so a burst of in-flight 429s counts as one signal."""
        now = self._clock()
        if now - self._last_slowdown < 1.0:
            return
        self._last_slowdown = now
        self.rate = max(self.min_rate, self.rate * 0.5)

    def note_success(self) -> None:
        """Slow additive-ish recovery toward the configured rate."""
        if self.rate < self.base_rate:
            self.rate = min(self.base_rate, self.rate * 1.05)

    @property
    def paused_for(self) -> float:
        return max(0.0, self._paused_until - self._clock())

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self._clock()
                if now < self._paused_until:
                    wait = self._paused_until - now
                else:
                    if self._resume_pending:
                        # Pause is over: restart the refill clock so callers trickle at the base rate.
                        self._resume_pending = False
                        self._tokens = 0.0
                        self._last = now
                    self._refill(now)
                    if self._tokens >= 1.0 - _EPSILON:  # tolerate float rounding so a tiny remainder never spins
                        self._tokens = max(0.0, self._tokens - 1.0)
                        return
                    wait = max((1.0 - self._tokens) / self.rate, _EPSILON)
            self.waited_s += wait
            await self._sleep(wait)


def parse_retry_after(value: str | None, *, now: Callable[[], datetime] | None = None) -> float | None:
    """Parse a ``Retry-After`` header: delta-seconds or an HTTP-date. Garbage -> ``None``."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        current = now() if now else datetime.now(UTC)
        return max(0.0, (when - current).total_seconds())
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return max(0.0, seconds)
