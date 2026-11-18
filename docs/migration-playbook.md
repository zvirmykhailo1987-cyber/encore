# Migration playbook

How I would move organizers from a legacy CRM to a new one, one organizer at a time, with live events running.

Each step is marked **Built** (code and a test exist in this project) or **Design** (a plan that has not run). Everything here was checked on synthetic data only. It needs adjusting once the real systems are known.

## The approach in one paragraph

No big-bang switch. Each organizer moves on its own, in waves, with a rehearsal first and a stated way back ([Strangler Fig](https://martinfowler.com/bliki/StranglerFigApplication.html), [Parallel Change](https://martinfowler.com/bliki/ParallelChange.html), [Stripe's online migrations](https://stripe.com/blog/online-migrations)). The legacy CRM stays the source of truth until the organizer's cutover. No organizer moves during its own on-sale or event.

## Phases

| # | Phase | What happens | Leaves behind | Status |
|---:|---|---|---|---|
| 0 | Outcomes | Agree what must carry over, and what old behaviour is deliberately left behind | Scope note | Design |
| 1 | Source profile | Field frequency, types, mixed types, sizes, orphans, per organizer | Profile report, data map | Design. The importer reports unmapped fields: **Built** |
| 2 | Data map | Each source field to a target field, or a recorded "not migrated" | `imports/mapping.py`, `MONGO_MAP` | **Built** for the demo shape |
| 3 | Dry runs | Full run on a copy, timed, writing nothing | Dry run report | **Built** |
| 4 | Pilot wave | A few friendly, small organizers with no event near | Lessons, corrected map | Design |
| 5 | Waves | Size by risk and event calendar. See below | Wave calendar | Design |
| 6 | Delta and shadow | Re-run for changes, compare reads between old and new | Mismatch list | Re-run: **Built**. Shadow reads: Design |
| 7 | Cutover | Per organizer. See the runbook below | Signed go or no-go | Report: **Built**. Runbook: Design |
| 8 | Hypercare | Old system read-only, watched, way back still open | Daily mismatch report | Design |
| 9 | Decommission | Prove zero traffic, delete, confirm deletion | Deletion confirmation | Design |

## What already works in code

| Need | How | Test |
|---|---|---|
| Re-run creates nothing | Stable key: organizer plus legacy id, row hash for change detection | `test_second_run_of_same_file_creates_nothing` |
| Kill at any moment, lose nothing | Rows, counters and checkpoint in one transaction | `test_killing_a_real_process_then_resuming_loses_and_duplicates_nothing` |
| Two organizers never merge | Uniqueness per organizer, every lookup scoped | `test_same_person_at_two_organizers_stays_two_separate_contacts` |
| Organizers run side by side | One writer per organizer, not one overall | `test_writers_are_limited_per_organizer_not_globally` |
| Broken rows wait for a person | Quarantine with reason, correction and retry | `test_quarantined_row_can_be_corrected_and_counters_stay_exact` |
| Independent proof | The file is read again and compared with the database | `test_reconciliation_detects_tampering_after_import` |
| Way back | Roll back the latest run to the exact earlier state | `test_rollback_restores_the_exact_prior_state_and_allows_reimport` |
| Corrected values survive | Curated fields are never overwritten | `test_import_does_not_overwrite_curated_field_or_erase_with_blank` |
| Erased people stay erased | Ledger checked on each load, replayed after a restore | `test_erased_people_stay_erased_after_a_restore_brings_them_back` |
| Consent proof travels | Time and wording move with the yes or no | `test_consent_proof_travels_with_the_contact_and_gaps_are_reported` |
| Card numbers stop here | A Luhn match in any column quarantines the row with no content kept | `test_a_card_number_in_any_column_is_quarantined_with_no_content_kept` |
| Deleted at source | Reported, not removed, so a partial file cannot cause mass deletion | `test_rows_gone_from_the_source_are_reported_and_not_deleted` |
| Go or no-go | A report read from the database, with an exit code for a pipeline | `test_cutover_report_says_no_until_every_check_passes` |

## Waves

An organizer's wave comes from three things, in this order:

1. **Event calendar.** Nobody moves within an agreed window around an on-sale or an event. A club with a season of matches moves between two fixtures.
2. **Risk.** Few contacts and simple use first. Many contacts, custom fields, integrations and reports later.
3. **Dependency.** Organizers sharing a group account move together.

Suggested growth: 5, then 50, then 500, then steady batches. Each step grows only if the last wave's numbers are clean. The real sizes depend on how many organizers actually use the CRM, which I do not know. Who decides: the product owner for the order, the on-call engineer can stop any wave at any time.

**Keeping one organizer's import from hurting another:**
- One writer per organizer. **Built**
- Imports have their own queue and workers, so ticket sync and check-ins cannot wait behind them. **Built** ([why](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/noisy-neighbor.html))
- `ENCORE_IMPORT_BATCH_PAUSE_MS` slows imports down. Any run can be paused. **Built**
- A rule that imports pause platform-wide during a large on-sale. Design

## Cutover runbook, for one organizer

Rehearse this as closely as possible several times before the first real one ([mock cutovers](https://learn.microsoft.com/dynamics365/guidance/implementation-guide/prepare-go-live-cutover-strategy)). Each step has one owner and one backup.

| Step | Action | Abort if |
|---:|---|---|
| 1 | T-7 days: dry run on a fresh export, compare with the previous one | Rejected rows grow by more than the agreed amount |
| 2 | T-7 days: import, reconcile, a person reviews quarantine | Reconciliation does not balance |
| 3 | T-1 day: organizer checks a sample and agrees the time | Organizer declines, or an on-sale has been announced |
| 4 | T-0: legacy CRM read-only for this organizer | The lock cannot be taken in time ([precedent](https://shopify.engineering/mysql-database-shard-balancing-terabyte-scale)) |
| 5 | Final export, delta import | Delta is far larger than expected |
| 6 | Replay the erasure ledger, reconcile again | Any discrepancy |
| 7 | `python manage.py cutover_report <organizer>` | Exit code 1 |
| 8 | Flip this organizer's routing to the new CRM | Point of no return is step 10, not here |
| 9 | Smoke test: search, open a contact, segment, send a test | Any failure: flip routing back |
| 10 | First real write in the new CRM. **From here, going back means carrying those writes back** | |
| 11 | Hypercare. Keep legacy read-only for the agreed time | |
| 12 | Decommission when traffic has been zero for the agreed time | Any traffic to the old store |

**Go or no-go checks.** These five are computed now: a dry run came first, a write import completed, no run is unfinished, reconciliation balanced, open quarantine is at most 0.5% of rows (a starting value, to be agreed). A person also decides on three reported numbers: opt-ins without proof, rows in the database but not in the source, and columns not migrated.

**Stopping.** Anyone on the migration can stop a wave. Restarting needs the owner. Reconciliation failure stops all cutovers until it is explained, because a data loss may be a personal data breach ([EDPB 9/2022](https://www.edpb.europa.eu/system/files/documents/2023-04/edpb_guidelines_202209_personal_data_breach_notification_v2.0_en.pdf)). My reading, for a DPO to confirm.

## The way back

| When | How | Cost |
|---|---|---|
| Before step 8 | `rollback` restores the exact earlier state. Only the latest run per organizer | Minutes |
| Steps 8 to 9 | Flip routing back. Nothing has changed in the new system | Minutes |
| After step 10 | Writes made in the new CRM must flow back, so the old system stays usable. **Design only.** Needs a reverse sync, as [Notion prepared](https://www.notion.com/blog/sharding-postgres-at-notion), or an accepted loss agreed with the organizer | Depends on how long it has been. It should be decided before the first cutover |

Rollback has been rehearsed in tests. A reverse sync has not been built.

## Shadow reads (design)

Between delta and cutover, send a sample of real reads to both systems. Users get the old answer. Differences are logged and sorted, because many will be false alarms from timing ([GitHub's Scientist](https://github.blog/engineering/engineering-principles/move-fast/)). A raw mismatch count is not an error count. A person reviews them by kind until the real ones reach zero.

## Reconciliation

It is out of band: separate code from the writer, reading the source again. In a real team, someone other than the import's author should write it, as [Notion did](https://www.notion.com/blog/sharding-postgres-at-notion). Here both came from one source, which is a weakness.

| Check | Vocabulary | Status |
|---|---|---|
| Rows in the source, missing in the database | MISSING_TARGET | **Built**, counts as a discrepancy |
| Rows in the database, not in this file | MISSING_SOURCE | **Built**, reported only |
| Content differs | RECORD_DIFF | **Built**, by hash and email |
| Counters match the rows | | **Built**, and enforced by a database constraint |
| A stop threshold | | Go or no-go check. Design for stopping mid-run |

Off-the-shelf tools may not help: [AWS DMS validation](https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Validating.html) does not list MongoDB as a source and does not support consolidating databases, so a tenant consolidation from MongoDB would need its own reconciler.

## If the legacy CRM is on MongoDB

Meteor stores data in [MongoDB](https://docs.meteor.com/api/collections.html). I do not know whether this CRM does. If it does:

| Trap | Handling | Status |
|---|---|---|
| `_id` is a random string by default, not ObjectId, and can be mixed | Keep as text, any shape | **Built** |
| `mongoexport` defaults to relaxed JSON, which can lose types | Export with `--jsonFormat=canonical`, read from a secondary | Parser handles both: **Built**. Export setup: Design |
| `$oid`, `$date`, `$numberLong` wrappers, and old dates kept as milliseconds | Unwrapped | **Built** |
| Fields differ between documents | Sample 1,000 documents, report unknown keys | **Built** |
| Missing, null and empty string mean different things | Target has NULL. The source difference is lost today | Known gap |
| Secrets inside documents, such as `services.password` | Never read, reported as unmapped | **Built** |
| Denormalised values drift | Recompute and compare, treat the copy as a candidate | Design |
| A long export is not a point-in-time snapshot | Run a delta after it, or use change streams | Design |
| Search index | New index, backfill, replay, swap the alias, and apply erasures to it ([reindex](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reindex-indices), [aliases](https://www.elastic.co/docs/manage-data/data-store/aliases)) | Design |

## What I would need to know first

The legacy CRM's real data store and size per organizer. How many organizers use it. Whether contacts belong to the organizer or to a shared buyer account, since that changes the merge and legal analysis. Who the new CRM's owner is. How events are scheduled across organizers. Whether the legacy system can be made read-only per organizer.
