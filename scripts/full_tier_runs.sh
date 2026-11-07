#!/usr/bin/env bash
# Repeat every spike on the full stack. Results replace those in docs/benchmarks.json.
# Usage: bash scripts/run_benchmarks.sh --stack-only bash scripts/full_tier_runs.sh
set -uo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."

PYTHON=python
[ -x .venv/Scripts/python ] && PYTHON=.venv/Scripts/python
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python

# Each scenario starts from a fresh Django and an empty edge cache, so one run cannot affect the next.
spike() {
  echo "=== $1"
  docker compose restart web edge >/dev/null
  sleep 3
  "$PYTHON" scripts/spike_bench.py "$@" --browser
  echo "exit $?"
}

spike "Refresh spike, 5,000 fans, no edge" direct 5000
spike "Refresh spike, 5,000 fans" edge 5000
spike "Refresh spike, 10,000 fans" edge 10000
spike "Refresh spike, 20,000 fans" edge 20000
for scans in 300 600 1,000 1,500; do
  spike "Doors open, 10,000 fans and $scans scans per second" edge 10000 --scans "${scans/,/}"
done
spike "Django stopped during a spike, 5,000 fans" edge 5000 --kill-web 40
docker compose up -d --wait web

echo "=== Idle connections"
docker compose restart web >/dev/null; sleep 3
"$PYTHON" scripts/idle_connections_probe.py 64

echo "=== Recovery after a flood that bypasses the edge"
bash scripts/recovery_probe.sh
