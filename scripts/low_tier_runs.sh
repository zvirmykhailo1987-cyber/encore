#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
# Repeat the measurements with docker-compose.low.yml. Results go to docs/benchmarks-low.json.
# Usage:
#   export COMPOSE_PATH_SEPARATOR=: COMPOSE_FILE=docker-compose.yml:docker-compose.low.yml
#   bash scripts/run_benchmarks.sh --stack-only bash scripts/low_tier_runs.sh
set -uo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."

PYTHON=python
[ -x .venv/Scripts/python ] && PYTHON=.venv/Scripts/python
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python

export ENCORE_BENCH_OUT=docs/benchmarks-low.json
export ENCORE_BENCH_MACHINE="Docker limits: 2 shared CPU cores, 2 GB split between services, 1 Gunicorn worker, 1 Celery worker; load generator on other cores"
rm -f "$ENCORE_BENCH_OUT"
STOP=$(mktemp -u)

health() {
  for service in db redis web worker worker-ticketing edge; do
    id=$(docker compose ps -aq "$service")
    echo "$service oom_killed=$(docker inspect -f '{{.State.OOMKilled}}' "$id") status=$(docker inspect -f '{{.State.Status}}' "$id") peak_mb=$(( $(docker exec "$id" cat /sys/fs/cgroup/memory.peak) / 1048576 ))"
  done
}

# Each scenario starts from a fresh Django and an empty edge cache, so one run cannot affect the next.
# A probe keeps asking Django directly, to tell "served fresh" from "edge serving old pages".
spike() {
  echo "=== $1"
  docker compose restart web edge >/dev/null
  sleep 3
  (
    ok=0; bad=0
    while [ ! -f "$STOP" ]; do
      if [ "$(curl -s -m 4 -o /dev/null -w '%{http_code}' -H 'Host: localhost' http://127.0.0.1:8001/api/v1/events/LOADTEST/availability)" = 200 ]; then ok=$((ok + 1)); else bad=$((bad + 1)); fi
      sleep 4
    done
    echo "django probe ok=$ok failed=$bad"
  ) &
  probe=$!
  "$PYTHON" scripts/spike_bench.py "$@" --keep-failed --browser
  echo "exit $?"
  touch "$STOP"; wait "$probe"; rm -f "$STOP"
  health
}

spike "Low: 5,000 fans, Django directly" direct 5000
spike "Low: 5,000 fans" edge 5000
spike "Low: 10,000 fans" edge 10000
spike "Low: 20,000 fans" edge 20000
spike "Low: 10,000 fans and 75 scans per second" edge 10000 --scans 75
spike "Low: 10,000 fans and 150 scans per second" edge 10000 --scans 150
spike "Low: 10,000 fans and 300 scans per second" edge 10000 --scans 300
spike "Low: 10,000 fans and 600 scans per second" edge 10000 --scans 600
spike "Low: Django stopped, 5,000 fans" edge 5000 --kill-web 40
docker compose up -d --wait web

echo "=== Import 1,000,000 rows"
"$PYTHON" scripts/benchmark.py import 1000000
echo "exit $?"
health

echo "=== Idle connections"
docker compose restart web >/dev/null; sleep 3
"$PYTHON" scripts/idle_connections_probe.py 64

echo "=== Recovery after a flood that bypasses the edge"
bash scripts/recovery_probe.sh
