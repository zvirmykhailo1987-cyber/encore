# Incident review: Django hung for 4 minutes after a flood

A real problem, found during testing rather than in production. Written in the blameless format I would use on a team.

**Date:** 7 November 2026. **Impact:** test environment only. **Detected by:** a load test on a smaller machine (2 cores, 2 GB).

## Summary

After a flood sent directly to Django had ended, Django answered nothing for 243 seconds, while using almost no CPU. On a live system this would be a full outage long after the traffic had gone.

## Timeline

| Step | What happened |
|---:|---|
| 1 | Ran the load tests in sequence on the small machine |
| 2 | The direct flood scenario ended. The next scenarios, through the edge, showed 100% and 47% failures |
| 3 | First reading: the small machine cannot cope. **Wrong.** Fans through the edge did fine once Django was restarted between scenarios |
| 4 | Measured the time to answer after a flood: 243 s, at low CPU. So not overload. Something was waiting |
| 5 | Stack dump of the Gunicorn worker with py-spy: threads blocked waiting for data on new connections |
| 6 | Read Gunicorn 26's worker. A thread waits up to 5 s on a new connection that has sent nothing |
| 7 | Reproduced alone: 64 idle connections delayed a real request by 79.8 s. That is 64 × 5 s ÷ 4 threads |
| 8 | First fix: wait 0 s. The hang was gone |
| 9 | Remeasuring, gate scans began to fail, from 2% to 45%. nginx logged the upstream closing early on POST. GET was hidden by nginx's own retry. **Stopped the run** |
| 10 | Compared values. 0 s: 49% of scans failed. 0.02 s: 0% failed, and the probe answered in 0.03 s |
| 11 | Chose 0.02 s, added a test that fails if the constant moves, and measured everything again |
| 12 | 18 November: went back to the open question. Read Gunicorn 26's `_keepalive_after`: the same constant is also the deadline for draining a request body before keeping the connection alive |
| 13 | So at 0 s the drain deadline has already passed: Gunicorn closes every connection after one request. nginx reuses a pooled upstream connection, finds it closed, retries the GET and gives up on the POST |
| 14 | Also found: Gunicorn keeps an idle connection for 2 s, nginx for 60 s. The edge could reuse a socket Django had just closed. That explains the few failed scans seen at saturation |
| 15 | Reproduced both with a real Gunicorn in a test: 20 POSTs with bodies on one connection, then a 3 s pause, then one more. The old settings fail it, both at 0.02 s and at 0 s |
| 16 | Fixed both: the drain gets its own 2 s deadline, Gunicorn's keep-alive is 75 s, nginx closes idle upstream connections after 50 s, and only a gate scan, which is safe to repeat, is retried by nginx |

## Root cause

Gunicorn's threaded worker holds a thread while waiting for the first bytes of a new connection. Enough silent connections keep all four threads waiting in turn. A flood leaves such connections behind. **Exactly where those come from was not proven.** The idle probe reproduces the effect, not necessarily the same source.

**Why 0 s failed scans (found 18 November).** Gunicorn 26 uses one constant for two things: how long to wait for the first byte, and the deadline for draining an unread request body before reusing the connection. At 0 the drain deadline is already over, so every connection closes after one request. nginx keeps upstream connections in a pool and does not retry a POST on a connection that turned out closed. Separately, Gunicorn's 2 s keep-alive against nginx's 60 s let the edge reuse a socket Django had closed, which caused the rare scan failures at saturation.

## What went well

Measuring recovery as its own number. Reproducing the effect alone before changing anything. Noticing the regression in the fix.

## What went badly

The first explanation (small machine) was comfortable and wrong. The first fix was shipped into a remeasurement without testing writes.

## Actions

| Action | Status |
|---|---|
| `deploy/gunicorn.conf.py` sets the wait to 0.02 s | Done |
| A test guards the constant | Done. Runs on Linux only |
| Idle connection and recovery probes in the load scripts | Done |
| Find out why 0 s fails | Done. The drain deadline shared the constant; it now has its own (`BODY_DRAIN_SECONDS`) |
| Keep-alive order: the edge closes idle upstream connections before Django does | Done. nginx 50 s, Gunicorn 75 s, guarded by a test |
| A real Gunicorn test for keep-alive with request bodies and an idle pause | Done. `tests/test_gunicorn_config.py`, Linux only. Fails on the old settings |
| Gate scans retried by nginx after a reset, since a scan is safe to repeat | Done. Other POSTs are not retried |
| Check again on each Gunicorn upgrade, since this is an internal constant | Open |
| A real deployment always has nginx in front, which sends the request as soon as it connects | Done in this design |
