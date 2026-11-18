# Operations and handover

What an operations team needs to run this. It also serves as a skeleton for the handover to an external team, following the areas of a [Production Readiness Review](https://sre.google/sre-book/evolving-sre-engagement-model/).

It has run on a **single-node k3s cluster in Docker**, never on a production cluster. Results are in `docs/k8s-proof.json`.

## Deploy

```bash
kubectl create namespace encore
```

```bash
kubectl -n encore create secret generic encore-secrets --from-literal=ENCORE_SECRET_KEY=... --from-literal=ENCORE_OPERATOR_TOKEN=... --from-literal=TICKETING_WEBHOOK_SECRET=... --from-literal=POSTGRES_PASSWORD=...
```

```bash
kubectl apply -k deploy/k8s
```

To prove it end to end, `python scripts/k8s_proof.py` creates a cluster, deploys, kills the import worker, restarts Django under traffic, and removes the cluster.

| What | How | Why |
|---|---|---|
| Schema changes | A `migrate` Job. Web pods wait in an init container | One writer of the schema per release |
| Liveness | `/livez`, no database | [A backend outage must not restart every pod](https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/) |
| Readiness | `/healthz`, checks the database | Stops traffic to a pod that cannot serve |
| Host check | Probes and metrics are answered before it | They arrive with a pod IP as the host |
| Shutdown, web | `preStop` sleep 5 s, then Gunicorn finishes within 30 s | [Endpoint removal runs in parallel with termination](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) |
| Shutdown, import worker | Treated as a crash. Checkpoint is durable, the reaper notices, the run resumes itself up to 5 times | An import outlasts any grace period |
| Celery delivery | `acks_late` plus `reject_on_worker_lost`, and a lease so a second delivery cannot run twice | [Celery acks on a killed child otherwise](https://docs.celeryq.dev/en/stable/userguide/tasks.html) |
| Long tasks on Redis | Visibility timeout 12 h. A redelivered import finds its lease held and stops | [The default of 1 hour would redeliver](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html) |
| Separation | Separate workers for imports and for ticketing | An import must not delay check-ins |
| Disruption | PDBs on web and edge. HPA on web CPU | |
| Network | The database and queue accept only the app. Django accepts only the edge and monitoring | The proof tried from an unrelated pod |
| Secrets | Plain Kubernetes Secrets, created by hand | [Not encrypted in etcd by default](https://kubernetes.io/docs/concepts/configuration/secret/). Use an external store in production |

**Before production:** a managed PostgreSQL with backups, exports in object storage rather than a ReadWriteOnce volume, TLS at an ingress, a real image registry instead of `encore:local`, and resource numbers measured under real load. The current ones come from Docker measurements on another shape of machine. Scaling workers with [KEDA on queue length](https://keda.sh/docs/latest/scalers/redis-lists/) would need care: a list length does not appear to count running tasks, so scale-down could kill work. I have not tested it.

## Service levels to start from

These are proposals. Nothing has measured them in production, and the owners may choose differently.

| Service | What is measured | Starting target |
|---|---|---|
| Event page | Replies not 5xx, through the edge | 99.9% over 30 days |
| Gate scan | Answered in 500 ms, not 5xx | 99.9% during event hours |
| Check-in delivery | Oldest undelivered under 10 minutes | 99% of the time |
| Ticket sync | Last success under 5 minutes | 99% of the time |
| Import | A reconciliation that balances | 100%, any miss stops cutovers |

Paging on error budget burn, at [14.4 times over 1 h and 6 times over 6 h](https://sre.google/workbook/alerting-on-slos/) for a 99.9% target, needs request metrics from the edge or a mesh, which are not exported yet. The alerts below watch queue state and data delay, as [batch work should be](https://prometheus.io/docs/practices/alerting/). New alerts should [run without paging for a week first](https://sre.google/workbook/on-call/).

## Alerts and what to do

Rules: `deploy/k8s/alerts.yaml`, checked with `promtool`. Each `runbook:` value is a heading below. Nobody other than me has tried these, so the first job of a handover is for the new team to run them.

### import-stuck
`ImportWorkerSilent`. A running import has sent no heartbeat for a minute.
1. `kubectl -n encore get pods -l app=worker`. Pod gone or restarting: the reaper will mark the run interrupted within 30 s and resume it. Wait.
2. Pod running but silent: `kubectl -n encore logs deploy/worker --tail=100`, then delete the pod. Nothing is lost.
3. The run stops resuming after 5 attempts on purpose. Read the error before pressing Resume.

### import-failed
`ImportFailed`. Open the run in the console and read the error. A changed source file is refused on purpose: start a new run. Anything else, fix and Resume: it continues from the checkpoint.

### reconciliation-unbalanced
`ReconciliationUnbalanced`. **Stop all cutovers.** Read `reconciliation` on the run: `missing_sample`, `mismatch_sample`, `checks`. A loss, or one organizer's data showing in another, may be a personal data breach: go to the breach path in `docs/security-privacy.md` straight away. Roll back the run if it is the latest. Do not re-run until the cause is known.

### quarantine-backlog
`QuarantineBacklog`. Group by reason in the console. One reason dominating usually means a mapping error, so fix the map and re-run rather than correcting rows by hand.

### breaker-open
`TicketingBreakerOpen`. Check the provider's status. The breaker probes by itself, and nothing needs restarting. Check-ins wait in the outbox and gates keep working. Contact the provider if it lasts.

### sync-stalled
`TicketSyncStalled`, `TicketSyncBehind`. Is beat running? Is `worker-ticketing` running? Is the breaker open? Then check for rate limiting on the Integration screen. Sync restarts from its cursor by itself.

### outbox-delayed
`CheckinDeliveryDelayed`. Usually the same cause as breaker-open. Check `worker-ticketing`. Delivery is safe to repeat.

### dead-letters
`CheckinsGaveUp`, `DeadLettersOpen`. Read the message on the Integration screen. A 4xx usually means our data or our credentials. Fix, then resolve it. There is no replay button yet: this is a known gap.

### scan-conflicts
`ScanConflictsOpen`. Offline gates admitted one ticket twice, or a ticket that was refunded or unknown. It is a business decision for the organizer, not a fault. Nothing in the system needs repair. There is no review screen yet: read `ticketing_scanconflict`.

## Common jobs

| Job | How |
|---|---|
| Bad release | `kubectl rollout undo deploy/web`. Schema changes stay expand-then-contract, so old code still runs |
| Before an on-sale | Scale web and edge, pause imports or set `ENCORE_IMPORT_BATCH_PAUSE_MS`. Someone has to tell you it is happening: **needs an on-sale calendar** |
| Rotate secrets | New Secret, rollout restart. The operator token changes at once. Webhook rotation drops in-flight messages for now |
| Restore from backup | Restore, then `manage.py replay_erasures --ledger <copy>`, then re-run reconciliation for recent runs. **Never rehearsed on a real backup** |
| Erase a person | `manage.py erase_subject <organizer> <email>` |
| Go or no-go | `manage.py cutover_report <organizer>` |

[A backup counts only once it has been restored.](https://sre.google/sre-book/data-integrity/)

## Handover to an external team

Responsibility [moves progressively](https://sre.google/sre-book/evolving-sre-engagement-model/), with the developers as backup for a while.

| Stage | What happens | Exit when |
|---|---|---|
| Review | They read this, the architecture and the known limits | Their questions are answered in writing |
| Shadow | They watch on-call, we respond | They have seen a real alert and run each runbook in staging |
| Reverse shadow | They respond, we watch | A week with no help needed |
| Own | They respond, we are the escalation | The agreed period ends |

Agree before starting: hours and escalation, who may see personal data, what they may change alone, how they learn of on-sales, and the exit criteria. **An ops team with cluster-admin can read personal data, so it may count as a sub-processor** under [GDPR Art. 28(2)](https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32016R0679), which can require notifying customers. My reading, for the DPO to confirm.

## Known gaps

No request-rate or latency metrics from the app or edge. No tracing. No `/metrics` protection beyond the network policy and the edge refusing it. No replay button for dead letters. No review screen for scan conflicts. No backup job. Not run on more than one node.
