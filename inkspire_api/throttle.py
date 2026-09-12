# -*- coding: utf-8 -*-
"""Sliding-window counters for login attempts and generation requests.

Both stores are dictionaries in this process: they do not survive a restart and are
not shared between workers. A deployment running more than one worker needs a shared
backend here, otherwise the effective limit is the configured one times the number of
workers.
"""

from __future__ import annotations

import time
from collections import defaultdict


class SlidingWindow:
    """Counts events per key over the last `interval` seconds."""

    def __init__(self, limit: int, interval: int) -> None:
        self.limit = limit
        self.interval = interval
        self._events: dict[str, list[float]] = defaultdict(list)

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def count(self, key: str, now: float | None = None) -> int:
        """The events still inside the window, dropping the ones that have aged out."""
        now = time.monotonic() if now is None else now
        kept = [at for at in self._events[key] if now - at < self.interval]
        self._events[key] = kept
        return len(kept)

    def record(self, key: str, now: float | None = None) -> None:
        if self.enabled:
            self._events[key].append(time.monotonic() if now is None else now)

    def reset(self, key: str) -> None:
        self._events.pop(key, None)


class LoginThrottle:
    """Refuses logins from an address that keeps failing.

    Only failures are counted, and a successful login clears the count, so someone who
    mistypes a password twice and then gets it right starts from zero again.
    """

    def __init__(self, max_attempts: int, interval: int) -> None:
        self._window = SlidingWindow(max_attempts, interval)

    @property
    def enabled(self) -> bool:
        return self._window.enabled

    def blocked(self, key: str) -> bool:
        return self.enabled and self._window.count(key) >= self._window.limit

    def record_failure(self, key: str) -> None:
        self._window.record(key)

    def reset(self, key: str) -> None:
        self._window.reset(key)


class RateLimiter:
    """Caps how often one key may do something, counting every attempt."""

    def __init__(self, limit: int, interval: int) -> None:
        self._window = SlidingWindow(limit, interval)

    @property
    def enabled(self) -> bool:
        return self._window.enabled

    def consume(self, key: str) -> bool:
        """Records an attempt and reports whether it is within the limit."""
        if not self.enabled:
            return True
        if self._window.count(key) >= self._window.limit:
            return False
        self._window.record(key)
        return True
