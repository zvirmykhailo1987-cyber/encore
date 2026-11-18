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

**Why.** Measured directly, Django served about 1,430 requests per second, with p95 over a second at 5,000 fans. A person refreshing a page does not need numbers fresher than a second, so nginx answers almost all of those requests. `proxy_cache_lock` lets one request through per key, and `proxy_cache_use_stale` keeps the page up while Django is slow or down.

**Cost.** Numbers can be about a second old, or older while Django is down. Nothing that writes can use this: gate scans still reach Django, and they saturate at about 760 per second on the test machine.

## 14. The edge re-resolves Django's address

**Why.** The first version resolved `web` once at startup. After Django's container was recreated, every request failed with connection refused until nginx restarted. A measurement caught it: the load test showed 100% failures. `server web:8000 resolve` with a short resolver lifetime fixes it.

**Cost.** Needs nginx 1.27.3 or newer, and a small shared memory zone.

## 15. Worker threads do not wait for silent connections

**Why.** A test on a smaller machine found that Django stopped answering for 4 minutes after a flood had ended, using almost no CPU. Gunicorn 26 holds a worker thread for up to 5 seconds on a new connection that has sent nothing. With four threads, 64 idle connections delayed a real request by 80 seconds. Lowering the wait to 0.02 seconds brought that to 0.3 seconds, and recovery after a flood to 0 seconds.

**Cost.** Gunicorn has no setting for this, so `deploy/gunicorn.conf.py` changes a module constant, and a test fails if the constant moves. A wait of 0 once failed 49% of gate scans. The cause, found later: Gunicorn 26 also uses that constant as the deadline for draining a request body before keep-alive, so at 0 it closed every connection after one request, and nginx does not retry a POST. The drain now has its own deadline, set in the same file, and the same file sets Gunicorn's keep-alive above nginx's upstream keep-alive, so the edge always closes an idle connection first. A test runs a real Gunicorn and fails on the old settings. Both patches touch Gunicorn internals, so the test also checks that the patched names still exist.

## 16. Every contact belongs to one organizer

**Why.** A platform with many organizers holds their customers separately. The first version made email unique across the system, so two organizers with one customer would have merged. That joins two businesses' data, and a wrong merge is hard to undo. Uniqueness and every lookup now include the organizer, and one writer per organizer replaces one writer overall, so organizers can run side by side.

**Cost.** The same person at two organizers is two contacts, which is intended. Which organizer a caller may see is decided by its key, see 22.

Organizers, card number detection, the erasure ledger and the per-organizer reconciliation together made one import slower. On the same laptop, back to back, 1,000,000 rows went at 4,140 rows per second before these changes and 3,467 after, about 16% slower. The README table shows 3,698 for the same size: that is a later run on the same machine, and the spread between runs is of that order. Several organizers at once more than make up for it (`docs/benchmarks-parallel.json`).

## 17. Imports and ticketing have separate workers

**Why.** A long import held a Celery slot, and two of them could stop ticket sync and check-in delivery altogether. Now they have separate queues.

**Cost.** One more deployment to run.

## 18. A killed worker is the normal way an import stops on Kubernetes

**Why.** An import can run for longer than any grace period. Instead of trying to finish, it relies on what already exists: the checkpoint is durable, the reaper notices silence, and with `ENCORE_IMPORT_AUTO_RESUME` the run restarts itself, up to 5 times so that a crash loop waits for a person.

**Cost.** Up to about 30 seconds of delay, plus redoing the unfinished batch. A deploy interrupts running imports.

## 19. The outbox uses a lease, not a lock

**Why.** Holding a row lock during an HTTP call ties up a database connection for as long as the API is slow. Moving the message's due time forward and committing is enough: if the worker dies, the lease expires and another sends it with the same key.

**Cost.** A message can be sent twice, so the API must honour the idempotency key.

## 20. Rows gone from the source are reported, not deleted

**Why.** A partial file and a mass deletion look the same. Deleting automatically could wipe an organizer. Reconciliation counts them, and a person decides.

**Cost.** Records deleted in the legacy system stay until someone acts.

## 21. Offline gates: the person is already inside

**Why.** A device with no network must decide alone. The server cannot refuse afterwards, so the first upload counts as the check-in, and anything else becomes a conflict for review, including a ticket refunded after the device synced. [Scanners that work offline accept this risk.](https://docs.pretix.eu/en/latest/api/resources/checkin.html)

**Cost.** Two people can get in on one ticket. An organizer who cannot accept that needs an on-site server.

## 22. Keys with one scope each, closed by default

An organizer's staff should see that organizer's contacts and nobody else's. One shared operator token could not say that. Each key now has a scope: platform, organizer or gate. The default permission in settings refuses any caller without a key, so a new view is closed until someone opens it on purpose. Another organizer's run answers 404 rather than 403, so a key cannot even learn which run numbers exist. A key is 256 random bits and only its SHA-256 digest is stored, which is enough for a value nobody can guess, while passwords would need a slow hash.

Considered: Django users with sessions. That is the right next step for people, and it changes how the console signs in. For devices at a gate and for scripts, a revocable key is the usual answer either way.

Gate keys belong to an organizer too. The ticket feed does not say whose event a ticket is for, so the platform records it (`event_owner assign`). A gate then sees and admits tickets of its own organizer's events only, and another organizer's ticket or event answers exactly as one that does not exist, so a gate cannot even probe for them. A ticket of an event nobody has assigned is reachable with a platform key alone.

## 23. One writer per organizer at a time, for everything that writes contacts

Imports already had one running writer per organizer. Rollback, reprocessing a quarantined row and erasure did not take part, so an erasure could land between a batch's check of the erasure ledger and its insert, and the person would be created again. Each of them now takes a PostgreSQL advisory lock on the organizer for its transaction, as does every import batch. Organizers never wait for each other. A second import for a busy organizer waits and retries for up to an hour, and says so on screen, instead of staying pending without a word.

## 24. Known limits of this version

- The breaker's state is shared through the database and loaded per task. Saving merges with what other tasks saved meanwhile, and new failures may open it, but a task does not see another task's change until it starts again.
- The outbound rate limit is per task, so the real rate to the ticketing API is the limit times the tasks running at once. A shared limit would need Redis.
- A half-open breaker lets every waiting call try, not one.
- Replaying erasures after a restore needs the ledger copy that `export_ledger` writes outside the database. Taking that copy is a job to schedule, not built here.
- A webhook or dead letter for an event with no recorded owner cannot be attributed to an organizer, so erasure leaves it; the hourly retention job removes webhook events after 30 days.
- Static files are served by Django, which is enough for a demo. A production deployment should use a CDN or a dedicated static file server.
- Kubernetes has run on one node only, in Docker. See `docs/k8s-proof.json`.
- There are no personal user accounts. A key stands for a team or a device, so the access log names a key, not a person.
