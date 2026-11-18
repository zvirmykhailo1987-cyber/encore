# How I would work on this

A proposal. I have not seen the code or met the team, so the plan and every estimate below will change after the first week. Estimates are in working days, for one engineer, and they are guesses until the real system has been read.

## First 30, 60 and 90 days

| Period | Goal | Done when |
|---|---|---|
| Days 1 to 10 | Learn. Run the system. Read the import paths and the API client. Sit in on an event or on-sale. Fix something small | One small fix shipped, and a written map of what I found, including what I got wrong |
| Days 11 to 30 | Profile the legacy data for a few organizers. Write the data map. Add measurements where none exist. First timed dry run on a copy | A dry run report, and a list of the three riskiest gaps agreed with the team |
| Days 31 to 60 | Harden the import path. Pilot wave. Fix the worst integration failure | Pilot organizers moved, and reconciled |
| Days 61 to 90 | Waves growing. Load work before the next large on-sale. Runbooks handed to ops | A wave running without me, and a load test against the real peak |

## Without unnecessary rewrites

1. Measure before changing. Most slow paths have a small cause.
2. Change behind a seam, and leave the rest alone ([Strangler Fig](https://martinfowler.com/bliki/StranglerFigApplication.html)).
3. Add a test that pins current behaviour before moving it.
4. Ship small: expand, migrate, contract ([Parallel Change](https://martinfowler.com/bliki/ParallelChange.html)).
5. Rewrite only with a measured reason and an agreed way back.

An example from this project: COPY replaced `bulk_create` only after profiling showed it as the biggest cost. The Gunicorn fix is one small config file.

## Example breakdown: moving one data source

Not a commitment: this shows how I would split the work.

| Task | Estimate | Risk that would change it |
|---|---:|---|
| Profile source: fields, types, sizes, per organizer | 2 to 3 | No read access, or many schemas |
| Data map, reviewed by the product owner | 2 to 4 | Unclear meaning of old fields |
| Reader for the source format, with tests | 1 to 3 | Mixed types |
| Mapping, validation, quarantine reasons | 2 to 4 | Business rules nobody remembers |
| Dry run on a copy, timed, report | 1 | Copy is hard to get |
| Reconciliation adapted to the source | 2 to 3 | |
| Pilot organizer, first real run | 2 to 5 | The organizer's calendar |
| Fixes from the pilot | 3 to 8 | This is the widest range on purpose |
| **Total** | **15 to 31** | |

I give a range and say what would move it, and I update it when something changes.

## Progress reports

One short written note per week, in this shape:

> **Week 12. Migration: on track, one risk.**
> **Done:** wave 3 moved, 48 of 50 organizers. Reconciliation balanced for all 48.
> **Not done:** 2 organizers held back, because of an on-sale next week. Moved to wave 4.
> **Numbers:** 412 of 1,200 organizers moved. Open quarantine 0.2%. 0 rollbacks this week.
> **Risk:** the delta import takes longer than expected for large organizers, 3 h against a planned 1 h.
> **Need from you:** a decision on read-only length for club-b by Thursday.
> **Next:** wave 4, a fix for the delta speed.

These numbers are invented for the example.

## Zero-downtime schema changes in Django

| Change | How |
|---|---|
| Add a column | Nullable first. Deploy code that writes it. Backfill in batches ([`atomic = False`](https://docs.djangoproject.com/en/stable/howto/writing-migrations/)). Then add the constraint |
| Add an index | [`AddIndexConcurrently`](https://docs.djangoproject.com/en/stable/ref/contrib/postgres/operations/) in a non-atomic migration |
| Unique field | Three migrations: add nullable, populate, make unique |
| Remove a column | Stop using it, deploy, then drop it in a later release |
| Data and schema together | [Not in one migration on PostgreSQL](https://docs.djangoproject.com/en/stable/ref/migration-operations/) |

The migrations in this repo do not all follow this, because the tables were empty. `crm/migrations/0003_tenants_erasure_offline.py` swaps a unique constraint in one step, which would lock a large table. On real data it would be split as above.

## ORM performance

Query counts are tested: a batch of 100 rows makes the same number of queries as a batch of 10, and the run list does not grow with the number of runs. Query plans for the heaviest paths, on 10 million rows, are in `docs/query-plans.txt`. The raw `COPY` and `unnest` paths exist because profiling asked for them, and each has a decision record.

## GitLab to GitHub

This repo already uses GitHub Actions, but it has never run on GitHub (see limits). For the move itself, from [GitHub's docs](https://docs.github.com/en/migrations/using-github-enterprise-importer/migrate-from-gitlab/understand-migrations):

- **Tool:** [GitHub Enterprise Importer supports GitLab](https://github.blog/changelog/2026-08-03-migrate-from-gitlab-to-github-with-github-enterprise-importer/) for Enterprise Cloud, as of August 2026. Older posts saying otherwise are out of date.
- **Moves:** code, history, issues, merge requests with reviews, milestones, releases.
- **Does not move:** LFS objects, CI variables and secrets, pipelines, webhooks, required approvals, snippets, boards. Plan each.
- **[No delta](https://docs.github.com/en/migrations/using-github-enterprise-importer/migrate-from-gitlab/migrate-your-repositories):** freeze work on a repo during its production move.
- **Order:** inventory, dry run for every repo, move, reclaim users, then convert CI as a separate step using [Actions Importer](https://docs.github.com/en/actions/tutorials/migrate-to-github-actions/automated-migrations/gitlab-migration).
- **Rotate every secret** during the move, since they must be recreated anyway.
- **Recreate branch protection** before anyone merges.

I have not done this migration myself.

## The surrounding stack

I have no working experience with Node.js, Meteor, MongoDB, Elasticsearch or React beyond what is shown here. The MongoDB import path here was written from documentation and tested on fixtures, not against a real database.
