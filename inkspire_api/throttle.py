# -*- coding: utf-8 -*-
"""Login throttling, per client address.

Failed attempts are counted in a sliding window and the count is cleared by a
successful login, so a user who mistypes a password twice and then gets it right
starts from zero again.

The store is a dictionary in this process: it does not survive a restart and is not
shared between workers. A deployment running more than one worker needs a shared
backend here, otherwise the effective limit is the configured one times the number
of workers.
"""

from __future__ import annotations

import time
from collections import defaultdict


class LoginThrottle:
    def __init__(self, max_attempts: int, interval: int) -> None:
        self.max_attempts = max_attempts
        self.interval = interval
        self._failures: dict[str, list[float]] = defaultdict(list)

    @property
    def enabled(self) -> bool:
        return self.max_attempts > 0

    def _recent(self, key: str, now: float) -> list[float]:
        kept = [t for t in self._failures[key] if now - t < self.interval]
        self._failures[key] = kept
        return kept

    def blocked(self, key: str, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        return len(self._recent(key, now if now is not None else time.monotonic())) >= self.max_attempts

    def record_failure(self, key: str, now: float | None = None) -> None:
        if self.enabled:
            self._failures[key].append(now if now is not None else time.monotonic())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
