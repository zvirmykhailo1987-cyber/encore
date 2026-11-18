"""HTTP client for the external ticketing API. All reliability decisions live here."""

import datetime as dt
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

from .resilience import CircuitBreaker, RetryPolicy, TokenBucket

LATENCY_BUCKETS_MS = (25, 50, 100, 250, 500, 1000, 2500, 5000)


class ConnectorError(Exception):
    pass


class RetriesExhausted(ConnectorError):
    pass


class CircuitOpen(ConnectorError):
    pass


class PermanentError(ConnectorError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


@dataclass
class CallStats:
    counts: Counter = field(default_factory=Counter)
    latency: Counter = field(default_factory=Counter)

    def observe_latency(self, milliseconds: float) -> None:
        for bound in LATENCY_BUCKETS_MS:
            if milliseconds <= bound:
                self.latency[str(bound)] += 1
                return
        self.latency["inf"] += 1


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait. The header holds either a number of seconds or an HTTP date."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value) - dt.datetime.now(dt.UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


class TicketingClient:
    def __init__(
        self,
        base_url: str,
        breaker: CircuitBreaker | None = None,
        bucket: TokenBucket | None = None,
        policy: RetryPolicy | None = None,
        timeout: httpx.Timeout | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=timeout or httpx.Timeout(5.0, connect=2.0))
        self.breaker = breaker or CircuitBreaker()
        self.bucket = bucket
        self.policy = policy or RetryPolicy()
        self.sleep = sleep
        self.stats = CallStats()

    def close(self) -> None:
        self.http.close()

    def _send(
        self, method: str, path: str, params: dict | None, json: dict | None, headers: dict | None
    ) -> httpx.Response:
        started = time.monotonic()
        last_problem = "no attempt made"
        for attempt in range(1, self.policy.max_attempts + 1):
            if not self.breaker.allow():
                self.stats.counts["breaker_rejections"] += 1
                raise CircuitOpen("The ticketing API circuit is open")
            if self.bucket:
                self.bucket.acquire()
            self.stats.counts["requests"] += 1
            sent = time.monotonic()
            retry_after = None
            try:
                response = self.http.request(method, path, params=params, json=json, headers=headers)
            except httpx.TimeoutException as exc:
                self.stats.counts["timeouts"] += 1
                self.breaker.record_failure()
                last_problem = f"timeout: {type(exc).__name__}"
            except httpx.TransportError as exc:
                self.stats.counts["server_errors"] += 1
                self.breaker.record_failure()
                last_problem = f"transport: {type(exc).__name__}"
            else:
                self.stats.observe_latency((time.monotonic() - sent) * 1000)
                if response.status_code == 429:
                    # The dependency is healthy and asking us to slow down. That is not a failure.
                    self.stats.counts["rate_limited"] += 1
                    self.breaker.record_success()
                    retry_after = _retry_after(response)
                    last_problem = "HTTP 429"
                elif response.status_code >= 500:
                    self.stats.counts["server_errors"] += 1
                    self.breaker.record_failure()
                    last_problem = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    self.stats.counts["client_errors"] += 1
                    self.breaker.record_success()
                    raise PermanentError(response.status_code, response.text)
                else:
                    self.breaker.record_success()
                    self.stats.counts["successes"] += 1
                    return response
            if attempt == self.policy.max_attempts:
                break
            delay = self.policy.delay(attempt, retry_after)
            if time.monotonic() - started + delay > self.policy.deadline:
                raise RetriesExhausted(f"Deadline reached after {attempt} attempts: {last_problem}")
            self.stats.counts["retries"] += 1
            self.sleep(delay)
        raise RetriesExhausted(f"{self.policy.max_attempts} attempts failed: {last_problem}")

    def fetch_tickets(self, cursor: str, limit: int = 500) -> dict:
        response = self._send("GET", "/v1/tickets", {"cursor": cursor, "limit": limit}, None, None)
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorError("The ticket feed answered with something that is not JSON") from exc

    def post_checkin(self, ticket_external_id: str, idempotency_key: str) -> dict:
        """Safe to repeat because the key is stable. A POST without a key is never retried by this client."""
        if not idempotency_key:
            raise ValueError("A check-in needs an idempotency key")
        response = self._send(
            "POST",
            "/v1/checkins",
            None,
            {"ticket": ticket_external_id},
            {"Idempotency-Key": idempotency_key},
        )
        try:
            return response.json()
        except ValueError:
            # A 2xx means the check-in was recorded. An unreadable body does not undo that.
            return {}
