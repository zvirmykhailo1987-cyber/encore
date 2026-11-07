import random

from ticketing.resilience import CircuitBreaker, RetryPolicy, TokenBucket


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_backoff_grows_stays_under_cap_and_varies():
    policy = RetryPolicy(base_delay=1, max_delay=8)
    rng = random.Random(1)
    samples = [policy.delay(6, rng=rng) for _ in range(200)]
    assert max(samples) <= 8
    assert len({round(s, 3) for s in samples}) > 50
    assert all(policy.delay(1, rng=rng) <= 1 for _ in range(100))


def test_retry_after_is_a_floor():
    policy = RetryPolicy(base_delay=0.1, max_delay=0.2)
    assert policy.delay(1, retry_after=7, rng=random.Random(1)) == 7


def test_token_bucket_enforces_rate():
    clock = Clock()
    bucket = TokenBucket(rate=10, capacity=2, clock=clock, sleep=clock.sleep)
    for _ in range(12):
        bucket.acquire()
    # Two tokens were free. Ten more at ten per second take one second.
    assert abs(sum(clock.slept) - 1.0) < 1e-6


def test_breaker_opens_rejects_then_probes_and_closes():
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout=10, clock=clock)
    for _ in range(3):
        assert breaker.allow()
        breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.times_opened == 1
    assert not breaker.allow()
    clock.now += 10
    assert breaker.allow()
    assert breaker.state == "half_open"
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.consecutive_failures == 0


def test_failed_probe_reopens_for_a_full_timeout():
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=10, clock=clock)
    breaker.record_failure()
    clock.now += 10
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open"
    clock.now += 9
    assert not breaker.allow()


def test_success_resets_the_failure_streak():
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"
