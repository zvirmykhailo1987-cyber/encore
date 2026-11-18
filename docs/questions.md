# Questions you may have

## Hiring

### Did you build this yourself, and how much was AI?
AI wrote most of the code, under my direction. I set the requirements, chose the design, decided what to keep or refuse, and checked results through tests and measurements. Mistakes caught along the way are listed in [How it was built](#how-it-was-built).

### How long did it take?
About two days: 6 and 7 November 2026.

### Have you run a customer data migration in production?
No. This project has never handled real customers or real traffic. It shows how I approach the problem, not a production record.

### How much commercial Django experience do you have?
None before this project. The job asks for 5 to 7+ years and this does not replace that. It is offered as evidence of how I work, so you can decide what it is worth.

### Which parts of the job description have you done, and which only in this demo?
Everything in the [requirements table](#requirements) was done in this demo. None of it was done in production. My earlier IT work was running infrastructure and document automation at a district court from 2011 to 2015.

### How is your English in a technical discussion?
Intermediate, according to my CV. Comfortable in writing. A technical conversation will be slower than with a native speaker, and it is better tested live than described.

### When can you start, what are the contract terms, and are you open to on-call and event weekends?
Not covered in this pack. These are agreed directly.

### Can you explain this in two minutes to a non-engineer?
When a company changes CRM, its customers' data must move without loss, and it must keep selling tickets while it happens. This shows that move surviving crashes and being checked afterwards, plus a ticket system that survives its partner failing and huge crowds refreshing one page.

### Is any of this data real, or copied from another company?
No. All data comes from a generator with a fixed seed. No real company's name, code or data is used.

### What is the most dangerous assumption here?
That synthetic data resembles real data. The generator breaks rows in ways I thought of, and real data will break in others. That is why the first real step is profiling, not importing.

## Leadership

### Why should I believe these numbers?
Do not take them on trust. The raw results are in the pack, the scripts are in the code, and the generator's seed is fixed, so you can run them yourself. Each states its machine. They come from one run per scenario on a laptop, so they describe behaviour rather than production capacity.

### What would break first on our real data?
The data map. It is written for a demo shape. After that, whatever real rows do that I did not imagine: the quarantine exists to catch this without stopping a run. Volume is less of a worry, because memory stayed flat from 1 to 10 million rows.

### What would you do in your first 30 days, knowing nothing about our code?
Run the system, read the import paths and API client, sit in on a live event if possible, ship a small fix, then profile real data for a few organizers. The plan is in [How I would work](#delivery).

### How do you avoid a rewrite while still shipping a new CRM?
Move behind seams, one organizer at a time. Rewrite only with a measured reason and an agreed way back.

### How would you sequence customers, and who decides?
By event calendar first, then risk, then dependencies, starting small and growing only after clean numbers. The product owner sets the order, and anyone on the migration can stop it.

### When do you stop a migration, and who can say stop?
Whenever reconciliation fails to balance, delta is far larger than expected, or the go or no-go report says no. Anyone working on it can stop it. Only the owner restarts it.

### What does rollback cost after a customer has written to the new system for a week?
This is the hard case, and the answer is not built. Before the first real write, going back takes minutes. After it, those writes must flow back through a reverse sync, or a loss must be agreed. That choice should be made before the first cutover.

### How do you report progress honestly?
A short weekly note with what is done, what is not, real numbers, risks and what I need. Estimates are ranges with the reason for the range. There is an example in [How I would work](#delivery).

### What did the AI get wrong, and how did you find out?
Among others: email unique across all organizers, a first fix that failed 49% of gate scans, and an edge that stopped working after a restart. Each was found by a measurement or a review, not by the AI. All are in [How it was built](#how-it-was-built).

### What would you refuse to ship from this demo?
All of it with real personal data, until there are user accounts and per-organizer access. Also the Gunicorn constant until the cause of its failure at 0 is understood, and rollback until a reverse sync exists or the loss is agreed.

### How would you keep event operations safe during a Saturday festival while migrating?
Imports have separate workers and can be paused or slowed. Nobody moves near their own event. A pause across the platform during a large on-sale would be a rule, and needs an event calendar I do not have.

### How does this help in handing infrastructure to an external team?
Each alert has a runbook, and there is a staged handover. Nobody else has used them yet, so the first step is having that team run them. Their access to personal data needs a separate decision.

## Backend

### Show me the import idempotency key. What happens with two workers on one batch?
The key is organizer plus `system:legacy_id`, in `ContactAlias`, with a unique constraint. A row hash tells unchanged from changed. A lease token fences off a second worker: the batch is discarded if the lease moved, and one writer per organizer is enforced in the database.

### What happens if a worker is killed halfway through a batch?
That batch's transaction never commits, so rows, counters and checkpoint all stay at the last batch. A test kills a real process, and the Kubernetes proof kills a real pod.

### Why acks_late? What about the Redis visibility timeout with long imports?
`acks_late` with `reject_on_worker_lost` means a killed worker's task can be delivered again. Without the second setting, Celery acknowledges on a killed child. Redis redelivers after its visibility timeout, 1 hour by default, while the task still runs. So it is set to 12 hours, and a redelivered import finds its lease held and stops.

### How do you detect a row changed in the old system after you copied it?
Run the import again on a new export. The hash differs, so it updates, and it fills only what was not corrected by hand. Between exports, nothing notices: real-time capture is not built.

### How do you verify the data, and who wrote the verifier?
Reconciliation reads the source again and compares it with the database. It uses the same mapping function as the import and comes from the same author, which is a real weakness. A wrong map could fool both. A team should have someone else write it.

### How do you handle a contact with no email, or two with the same email in one organizer?
No email and no phone goes to quarantine. A phone alone is kept. Two rows with one email merge into one contact, filling only empty values, with both legacy ids kept. Merging by phone is not done, because families and companies share phones.

### What does your retry policy do on a 429 with Retry-After?
Waits at least that long, in seconds or as an HTTP date, with jitter otherwise, within a time budget. It does not count against the breaker, because a 429 is a healthy service asking for less.

### Where exactly is the timeout set on each outbound call?
`TicketingClient.__init__`: 5 s, with 2 s to connect, on one `httpx.Client` that every call uses. This value is a starting point. It should come from the real API's latency.

### What stops retries multiplying across layers?
Retries live only in `TicketingClient._send`. Tasks do not retry, and the outbox uses one attempt per run. There is no shared retry budget across workers, which is a known gap.

### How does your sync cursor handle deletes and clock skew?
Skew does not matter, because the cursor is the API's and not a clock. Order does not matter, because versions decide. Deletes are not handled unless they come as a status: known gap, and the first question for the real API's owner.

### Show the query plan and N+1 protection on the worst endpoint.
Plans on 10 million rows are in `docs/query-plans.txt`. A test checks that query counts do not grow with batch size or with the number of runs.

### How do you add an index or column on a huge table in Django?
See the table in [How I would work](#delivery): concurrent indexes, nullable first, batched backfill, contract later. This repo's own `crm/migrations/0003_tenants_erasure_offline.py` swaps a unique constraint in one step, which would be wrong on a large table.

### How do you enforce tenant isolation, and which test fails if a filter is missing?
Each lookup filters by organizer, and uniqueness includes it. Bug injection removes those filters and records which test fails: see the evidence section. It is application-level only, with no row-level security in the database, and the console does not separate organizers, because it has one token.

### What happens to the edge cache when it expires under a spike, and after the lock timeout?
One request refreshes, in the background, while the rest get the stale copy. A request that waits past the 3 s lock timeout goes to Django, and its reply is not cached. See [Live events](#live-events).

### What is not cacheable, and how do you know a personal page is never cached?
Only two read paths are cached, by pattern, for GET. The rest passes through untouched. Those two views use no user data.

### How would you map a Meteor collection with mixed types and string ids?
`_id` stays text in any shape, wrappers are unwrapped, unknown fields are reported, and a broken document goes to quarantine. Written from documentation and tested on fixtures, not against a real database.

### What did you refactor, and what did you leave?
This is new code, so there is no real legacy refactoring to show, and that is a limit of this evidence. `bulk_create` became `COPY` after profiling, and the outbox lock became a lease. The rest of the limits stayed, and they are listed.

### What is your test strategy, and which bug would your tests miss?
Real PostgreSQL, real killed processes, and failures injected against a real HTTP server, with bug injection to check the tests themselves. They would miss a bug in the shared mapping logic that makes the importer and the reconciler agree on something wrong, and anything only real data triggers.

## Live events and product

### What happens at the gate when the network or backend is down?
A gate with a downloaded manifest decides alone and uploads later. The server side of this is built and tested, but there is no scanner app and it has never run at a venue. See [Live events](#live-events).

### Two scanners, one ticket, both offline: what does the attendee see, and what do we see later?
Both people get in. Later, the second upload shows as a conflict with both scan ids. This is an accepted risk of plain offline scanning, and an on-site server would remove it.

### A ticket is refunded after the scanners last synced. What happens?
The person gets in, because the device could not know. On upload it becomes a `ticket_refunded` conflict, is not counted as a check-in, and a person decides.

### How do you stop overselling during an on-sale?
Not built: there is no checkout here. It would use a conditional update on stock with expiring holds, the same pattern as admission. See [Live events](#live-events).

### Fans refreshing all at once: what do they see, and is it fair?
They see numbers up to a second old, quickly. No queue exists, so there is no fairness to speak of. A real on-sale needs a waiting room, which I would buy rather than build.

### Can an organizer see migration status and approve their data?
Not today. The console is for operators. The go or no-go report is the base for one.

### What does an organizer's team notice on cutover day?
It depends on the new CRM, which I have not seen. Saved segments and reports are not covered here and would need their own map.

### Can you cut over between two events for a club with a season of matches?
That is the intended wave rule. The per-organizer design allows it. It has not been tried.

### How do imports affect an organizer with an on-sale at the same time?
Imports have their own workers, can be paused, and can be slowed per batch. They still share the database. The parallel measurement shows how that scales, and a real on-sale would need its own test.

### What about cashless balances and staff records?
Not in scope. I have not touched them and found no good public design to base a guess on.

## Operations

### What does the app do on SIGTERM, and how long does it need?
Web: a 5 s `preStop`, then Gunicorn finishes within 30 s. Ticketing worker: warm shutdown. Import worker: it may be killed after 30 s, on purpose. The run resumes from its checkpoint.

### What do the liveness and readiness probes check?
Liveness checks that the process answers, and does not touch the database. Readiness checks the database.

### How do migrations run on deploy, and what if two pods try at once?
A single Job runs them, and pods wait in an init container until `migrate --check` passes. PostgreSQL locks protect a parallel attempt, and the Job avoids it.

### What scales the Celery workers, without killing a running task?
Manually for now. An HPA on queue length is not set up, and I have not tested KEDA. Killing an import is safe here, since it resumes, but a shorter ticketing task should get a warm shutdown.

### What are the SLOs, and which alerts page someone at 3am?
Proposals are in [Operations](#operations). Those marked `severity: page` wake someone: unbalanced reconciliation, silent import worker, breaker open, sync stalled, delivery delayed. None has run in production.

### Where is the runbook for each alert, and has anyone else tried it?
In [Operations](#operations), one section per alert. A test checks alerts refer only to metrics that exist. Nobody else has tried them.

### How are secrets stored and rotated, and do you need access to personal data?
Kubernetes Secrets, created by hand, not in git. Production should use an external store. Rotation is a new Secret and a restart. Debugging should work from metrics and logs with no personal data, and the role's real access is your decision.

### How do I roll back a bad release, and has the restore been tested?
`kubectl rollout undo`. Restore has been simulated in a test for erasures only. A real backup has never been restored here.

### What resource requests and limits did you measure?
The small machine tests set memory for each service and watched peaks. The Kubernetes values are based on those, and need measuring again under real load.

### What do you need from us before you hand over, and what is the exit criterion?
Hours, escalation, data access, and the on-sale calendar. Exit: a week of reverse shadow with no help needed. See [Operations](#operations).

### Before an on-sale, what do we scale, and who tells us?
Web and edge up, imports paused or slowed. Who tells you is an open question, and it needs a calendar from the business.

### What breaks in CI moving from GitLab to GitHub?
Variables, secrets, pipelines, webhooks and required approvals do not move. There is a checklist in [How I would work](#delivery). I have not done this migration myself.

## Privacy and legal

### Who is controller and who is processor?
My reading: the organizer is controller, and the platform is processor. It changes if the platform runs a shared buyer account. This needs your DPO. The privacy page is an engineer's reading, not legal advice.

### Do you merge contacts across organizers?
No, and a test fails if the code does.

### How is proof of consent carried across?
The yes or no, the date and the wording move with the contact. Unknown stays unknown, and gaps are counted in the go or no-go report. Which legacy fields hold this is unknown until the real data is profiled.

### An erasure request arrives after the dry run but before cutover. What happens?
The contact is removed, quarantine is cleared, and a hash goes into a ledger. Any later run quarantines that person's rows with no content kept.

### If you restore a backup or roll back, how do erased people stay erased?
Rollback cannot bring them back, because their snapshots are removed with them. After a restore, `replay_erasures` applies the ledger, from a copy kept outside the database. That copy's storage is a decision still to make.

### Does the migration need a DPIA?
The controller decides, and I expect the platform to help. I could not read the WP248 criteria text directly, so I give no verdict.

### Does the external ops team become a sub-processor?
Possibly, if it can reach personal data, and cluster-admin usually can. It may require notifying customers. For the DPO.

### If a migration shows contacts to the wrong organizer, what happens in the first 72 hours?
See the breach path in [Security and privacy](#security-privacy): stop, tell the DPO, establish scope from the run id, contain, notify as decided, and write it down.

### Is data lost during migration a personal data breach?
Permanent loss is an availability breach under EDPB guidance. So I treat it as a candidate breach until shown otherwise, not as a plain bug. My reading.

### Where does staging data live, who can access it, when is it deleted?
Here, only synthetic data on a laptop. For real data, these are unanswered, and they would be answered before any real export is touched.

### How do you handle minors' records?
Not modelled. The real data may hold a date of birth, which would affect marketing eligibility. Thresholds vary by country.

### Could a card number be sitting in a notes field?
Yes. Any column is checked, and a match is quarantined with its content discarded. It can miss unusual formats.

### Does the old CRM data get deleted at the end, and who confirms it?
The plan: prove zero traffic, delete, and confirm in writing. This is a design and has not run.

### Does any data leave the EEA?
Unknown here. It depends on your hosting, ops team and tools.
