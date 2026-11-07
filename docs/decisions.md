# Design decisions

Each entry records what was chosen, why, and what it costs.

## 1. One transaction per batch: rows, counters and checkpoint together

**Why.** A worker can die at any instruction. If rows and checkpoint commit together, a resume starts exactly after the last durable batch, and the counters can never drift from the data.

**Cost.** One failed row cannot fail a batch, so row-level problems must be handled inside the batch: they go to quarantine. A long batch holds its transaction open, so batch size is bounded (default 5,000).

## 2. Restart point is a byte offset, not a row number

**Why.** Skipping N rows means re-parsing the whole file on resume. Seeking to a byte offset is instant at any file size. The reader tracks the offset after each record, including for quoted fields that span lines.

**Cost.** The file must not change between runs, so a run stores its SHA-256 and refuses to resume against a different file.

## 3. Identity lives in an alias table

**Why.** One person can appear under several legacy ids. `ContactAlias` maps each legacy record to one contact, and its `src_uid` is what makes a re-run create nothing. Row hashes separate "seen, unchanged" from "seen, changed".

**Cost.** One extra table and one lookup per batch.

## 4. Merge by email only, never by phone

**Why.** A shared family or company phone would merge different people, and a wrong merge is much harder to repair than a missed one. The conservative rule leaves a duplicate that a person can merge later.

**Cost.** The same person with two emails stays as two contacts.

## 5. Unknown stays NULL

**Why.** A missing opt-in is not a refusal, and a missing city is not an empty city. Marketing consent is the sharpest case: turning unknown into `False` or `True` changes what the business may legally do.

**Cost.** Nullable columns, and every reader must handle `NULL`.

## 6. Reconciliation re-reads the source

**Why.** Checking the counters only tests the code that wrote them. Reconciliation reads the file again from byte zero and compares it with the database, so it can catch a bug in the import itself. A test tampers with rows after the import and confirms it notices.

**Cost.** Extra time after each run: a second pass through the whole file.

## 7. COPY for new rows

**Why.** Profiling on 100,000 rows showed the ORM's `bulk_create` as the largest single cost. COPY with ids reserved up front removed most of it.

**Cost.** Raw SQL tied to PostgreSQL, and it bypasses model `save()` and signals. There are no signals on these models today.

## 8. Rollback only for the latest applied run

**Why.** Undoing an older run would restore values that newer runs may have built on. Refusing is simple and safe.

**Cost.** Undoing an older run requires rolling back newer ones first.

## 9. A transactional outbox for check-ins

**Why.** Writing a scan and calling an API cannot be one atomic action. The scan and its outgoing message commit together, and delivery happens later with a stable idempotency key. Delivery is at least once, and the key makes the effect once.

**Cost.** The external system sees check-ins slightly later and must support idempotency keys.

## 10. 429 does not trip the circuit breaker

**Why.** A 429 means a healthy service is asking for less traffic. Opening the breaker would stop sync entirely when it only needs to slow down. 5xx responses, timeouts and transport errors do count.

**Cost.** A service that answers only 429 is retried until the attempts run out, without the breaker's protection.

## 11. Version check inside the upsert

**Why.** A page sync and a webhook can race. Putting `WHERE version < EXCLUDED.version` in one SQL statement means an older version cannot overwrite a newer one, whichever commits first.

**Cost.** Raw SQL, tied to PostgreSQL.

## 12. Admit a ticket with a conditional update

**Why.** `UPDATE ... WHERE checked_in_at IS NULL` lets the database decide which of several simultaneous gates wins. A read followed by a write would admit two people.

**Cost.** None worth noting. A retry of the same scan is recognised by its scan id.

## 13. A one second edge cache for what fans refresh

**Why.** Measured directly, Django saturated at about 1,660 requests per second, with p95 over a second at 5,000 fans. A person refreshing a page does not need numbers fresher than a second, so nginx answers almost all of those requests. `proxy_cache_lock` lets one request through per key, and `proxy_cache_use_stale` keeps the page up while Django is slow or down.

**Cost.** Numbers can be about a second old, or older while Django is down. Nothing that writes can use this: gate scans still reach Django, and they saturate at about 840 per second on the test machine.

## 14. The edge re-resolves Django's address

**Why.** The first version resolved `web` once at startup. After Django's container was recreated, every request failed with connection refused until nginx restarted. A measurement caught it: the load test showed 100% failures. `server web:8000 resolve` with a short resolver lifetime fixes it.

**Cost.** Needs nginx 1.27.3 or newer, and a small shared memory zone.

## 15. Worker threads do not wait for silent connections

**Why.** A test on a smaller machine found that Django stopped answering for 4 minutes after a flood had ended, using almost no CPU. Gunicorn 26 holds a worker thread for up to 5 seconds on a new connection that has sent nothing. With four threads, 64 idle connections delayed a real request by 80 seconds. Lowering the wait to 0.02 seconds brought that to 0.3 seconds, and recovery after a flood to 0 seconds.

**Cost.** Gunicorn has no setting for this, so `deploy/gunicorn.conf.py` changes a module constant, and a test fails if the constant moves. A wait of 0 failed 49% of gate scans in a short measurement. The cause of that was not found, so 0.02 is a measured choice rather than an explained one. Gunicorn also closes a connection that stays silent for over 2 seconds, which is fine behind nginx, since nginx sends a request as soon as it connects.

## 16. Known limits of this version

- The outbox holds a row lock during the HTTP call. That is acceptable at demo volume. A production version should claim messages with a lease instead.
- The breaker's state is shared through the database and loaded per task. It is not updated across workers during a task.
- Static files are served by Django, which is enough for a demo. A production deployment should use a CDN or a dedicated static file server.
- Kubernetes is not covered. This has only run under Docker Compose.
- There is no user management. Writes use one operator token, and reads are public, so real personal data does not belong here.
- `Retry-After` is read in its seconds form only. The HTTP date form is ignored, and normal backoff applies.
