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

## Root cause

Gunicorn's threaded worker holds a thread while waiting for the first bytes of a new connection. Enough silent connections keep all four threads waiting in turn. A flood leaves such connections behind. **Exactly where those come from was not proven.** The idle probe reproduces the effect, not necessarily the same source.

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
| Find out why 0 s fails | **Open** |
| Check again on each Gunicorn upgrade, since this is an internal constant | Open |
| A real deployment always has nginx in front, which sends the request as soon as it connects | Done in this design |
