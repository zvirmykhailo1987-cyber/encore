# Ticketing API integration

The connector talks to a stand-in API with switchable failures, because I have no access to a real one. Everything real that would change is in `ticketing/client.py`, `ticketing/services.py` and `TICKETING_BASE_URL`. The API this system itself offers is described in `docs/openapi.yaml`.

## What happens on each failure

| Failure | Behaviour | Where | Test |
|---|---|---|---|
| Slow response | 5 s read and 2 s connect timeout, on every call | `client.py`, `httpx.Timeout` | `test_slow_response_times_out_and_is_retried` |
| 429 | Retry, waits at least `Retry-After` in seconds or as an HTTP date. Does not count against the breaker | `client.py`, `_retry_after` | `test_retry_after_header_is_honoured`, `test_retry_after_is_understood_as_an_http_date` |
| 5xx | Retry with capped exponential backoff and full jitter, inside a time budget | `resilience.py`, `RetryPolicy` | `test_retries_through_429_and_500_and_counts_them` |
| Other 4xx | No retry. It goes to the dead letter list | `client.py`, `PermanentError` | `test_client_errors_are_not_retried` |
| API down | Breaker opens, stops traffic, probes, recovers by itself | `resilience.py`, `CircuitBreaker` | `test_outage_opens_breaker_and_stops_traffic_then_recovers` |
| Duplicate event | Ignored by event id | `services.py`, `handle_webhook` | `test_webhook_duplicate_and_out_of_order` |
| Out of order event | An older version cannot overwrite a newer one, enforced inside one SQL statement | `apply_ticket_versions` | `test_older_version_never_overwrites_newer` |
| Malformed item | Dead letter, and the rest of the page continues | `sync_tickets` | `test_full_sync_under_chaos_matches_the_source_exactly` |
| Crash mid-sync | Page and cursor commit together, so at most one page repeats | `sync_tickets` | `test_sync_stops_at_outage_and_continues_from_the_cursor` |
| Crash mid-delivery | The lease runs out, another worker sends with the same key | `flush_outbox` | `test_worker_dying_mid_call_leaves_a_lease_that_expires_and_sends_the_same_key` |

Some test names above belong to `tests/test_connector.py`. The full list is in the test evidence section.

## Retries are in one place

Retries multiply across layers: five layers of three attempts is 243 ([AWS](https://builder.aws.com/content/3EumjoZascWd1oZiEgL8ORlv3qE/timeouts-retries-and-backoff-with-jitter)). So they live in one place only: `TicketingClient._send`. Celery tasks do not retry. The outbox runs the client with one attempt and owns its own schedule.

**Not built:** a shared retry budget across workers ([SRE](https://sre.google/sre-book/addressing-cascading-failures/)). Each call has an attempt limit and a deadline, and the breaker stops a dead dependency, but a partial outage could still see more retries than it should.

## Idempotency

| Direction | Key | Same key, same content | Same key, different content | Kept for |
|---|---|---|---|---|
| Outgoing check-in | Scan id, in `Idempotency-Key` | The API should treat it as one | Not sent | As long as the outbox row |
| Incoming gate scan | `scan_id` | Same answer as the first time | 422 `scan_id_reused`. Admits nobody | As long as the ticket |
| Incoming webhook | Event `id` | `duplicate` | Not compared | No expiry yet. **A real system needs a retention job** |

The outgoing side depends on the real API honouring a key. If it does not, delivery is at least once with possible duplicates, and that has to be discussed with its owner. [Stripe's description](https://docs.stripe.com/api/idempotent_requests) is the model. A POST with no key is refused by the client.

## Webhooks

HMAC-SHA256 over `timestamp.body`, on the raw bytes, compared in constant time, with a 5 minute tolerance. Tests cover forged and old messages. Processing currently runs inside the request rather than on a queue, which is fine at this size. **Not built:** secret rotation with two valid secrets, as [Standard Webhooks](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md) describes.

## Sync

A page and its cursor commit together, and a worker that lost the race backs off. **Not handled:** tickets deleted upstream with no event. A real feed needs a tombstone, an `include_deleted` style option, or a periodic full comparison. Cancelled and refunded tickets are handled as status changes.

## Knowing it is healthy

The console's Integration screen, plus metrics for breaker state, cursor lag, seconds since last success, oldest undelivered check-in and open dead letters. Alerts are in `deploy/k8s/alerts.yaml`.

## Replacing the stand-in

1. Ask the API's owner about rate limits, versioning, idempotency, webhooks, deletions and sandbox access.
2. Write a contract test against its sandbox.
3. Map its fields in `parse_ticket`, and its cursor in `sync_tickets`.
4. Set `TICKETING_BASE_URL` and `TICKETING_IS_MOCK=0`, which also disables fault injection.
5. Start on one test event, with sync only and delivery off.

## Known limits

- Errors have an `error` field, not [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html) Problem Details. A small change, not done.
- No `RateLimit` response headers on this API. That header is [still an IETF draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/).
- The token bucket is per process, so the real rate is that number times the number of workers.
- Breaker state is shared through the database and loaded per task, so workers do not see it change during a task.
