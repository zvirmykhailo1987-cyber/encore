# How this was built, and what went wrong

**AI wrote most of the code**, under my direction, in about two days (6 and 7 November 2026). I set the requirements and chose the design. I decided what to keep and what to refuse. The checks below are how the output was held to account. Doubt is reasonable: [more developers distrust AI output than trust it](https://survey.stackoverflow.co/2025/ai), and "almost right" is the most common complaint. So this page gives evidence you can check rather than a claim.

## What it does not show

This project does not show years of production experience, and it should not be read that way. It has never met real users, real data or a real on-call night. What it shows is how the problems in this job were approached, and that the results were checked. You can judge that part yourself.

## Mistakes caught along the way

These really happened, and each changed the code.

| What went wrong | How it was found | What changed |
|---|---|---|
| Contacts were unique by email across the whole system. With many organizers, that would have merged two organizers' customers | Reviewing the project against the job's multi-organizer reality, after the first version looked finished | Everything scoped by organizer, with tests and bug injection. Decision 16 |
| A long import could hold up ticket sync, because both shared workers | The same review | Separate queues. Decision 17 |
| Django hung for 4 minutes after a flood ended, at almost 0% CPU | A load test on a smaller machine | Root cause in Gunicorn 26. See `docs/incident-gunicorn.md` |
| The first fix for that hang (a wait of 0) broke gate scans: 49% failed in one short test | Noticed while reporting progress, in the numbers of the next run | Measured three values and chose 0.02 s. The reason 0 fails is still unknown |
| nginx was killed for memory at 20,000 fans | The same small machine test | Connection limit halved, memory limit raised |
| Load tests did not ask for compression, unlike browsers | Review of the load script | All numbers measured again |
| Tickets and outbox looked unequal after a run: 3,726 against 3,727 | The benchmark's own check | Not a bug in the data: counts were read while Django was finishing. The check now waits |
| Edge kept the old Django address after a restart, 100% failures | A load test | Decision 14 |
| The outbox held a database lock during each HTTP call | Listed as a known limit, then fixed | A lease. Bug injection confirms the test catches its removal |
| The network policy blocked the migration job from the database, so deployment hung | The first real run on a cluster | The job got its own label and rule |
| A format name was too long for its column | The first test on real PostgreSQL | Renamed |
| Offline uploads admitted refunded tickets as normal entries | Going through questions a live-events engineer would ask | They now become conflicts |

## What checks the code

| Check | What it answers | Result |
|---|---|---|
| Tests against real PostgreSQL | Does it do what it says? | See the evidence section |
| Bug injection (`scripts/mutation_check.py`) | Would the tests notice? Each injected bug removes one guarantee | `docs/mutation-results.json` |
| Kill tests | Real process killed mid-import, real pod killed in a cluster | Passing |
| Load tests (k6) | Where does it break? | Raw results in `docs/` |
| Reconciliation | Independent re-read of the source | 0 discrepancies, except where a test tampers on purpose |
| ruff, mypy, secret scan | Basics | Clean |

## What a reviewer should distrust

1. **The importer and its reconciler share one source of ideas,** and partly one mapping function. A wrong map could fool both, because reconciliation uses the same `clean_row`. A real team should have someone else write it.
2. **Synthetic data is too clean.** The generator breaks rows in ways I thought of. Real data will break in others.
3. **One run per load scenario, on a laptop,** with the generator on the same machine.
4. **The stand-in API was written by the same author as the client.** It tests my assumptions about failure, not a real API.
5. **The hack in `deploy/gunicorn.conf.py`** changes a module constant, chosen by measurement, without knowing why 0 fails.

## Would I ship it?

No. Not with real personal data: there are no user accounts or per-organizer access, among the limits listed in `docs/security-privacy.md` and `docs/operations.md`. It is a demonstration of approach.
