# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Reliability primitives with no I/O of their own. Clock and random source are injected for exact tests."""

import random
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.2
    max_delay: float = 10.0
    #: Total time budget for one logical call, so retries cannot pile up behind a dead dependency.
    deadline: float = 30.0

    def delay(
        self, attempt: int, retry_after: float | None = None, rng: random.Random | None = None
    ) -> float:
        """Full jitter, so workers that failed together do not retry together. Retry-After is a floor."""
        ceiling = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        jittered = (rng or random).uniform(0, ceiling)
        return max(jittered, retry_after) if retry_after is not None else jittered


class TokenBucket:
    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self.updated = clock()

    def acquire(self) -> float:
        """Take a token, waiting if needed. Returns the seconds waited."""
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        self.tokens -= 1
        if self.tokens >= 0:
            return 0.0
        wait = -self.tokens / self.rate
        self.sleep(wait)
        return wait


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 15.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.clock = clock
        self.state = self.CLOSED
        self.consecutive_failures = 0
        self.opened_at: float | None = None
        self.times_opened = 0

    def allow(self) -> bool:
        if self.state == self.OPEN:
            if self.opened_at is not None and self.clock() - self.opened_at >= self.reset_timeout:
                self.state = self.HALF_OPEN
                return True
            return False
        return True

    def record_success(self) -> None:
        self.state = self.CLOSED
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.state == self.HALF_OPEN or self.consecutive_failures >= self.failure_threshold:
            if self.state != self.OPEN:
                self.times_opened += 1
            self.state = self.OPEN
            self.opened_at = self.clock()
