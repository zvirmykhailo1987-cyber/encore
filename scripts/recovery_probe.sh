#!/usr/bin/env bash
# How long does Django stay unresponsive after a flood that bypasses the edge?
# Usage: bash scripts/recovery_probe.sh [fans] [seconds at peak] [longest wait]
set -uo pipefail
cd "$(dirname "$0")/.."
PYTHON=python
[ -x .venv/Scripts/python ] && PYTHON=.venv/Scripts/python
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python
fans=${1:-5000}; hold=${2:-20}; limit=${3:-900}

docker compose restart web edge >/dev/null
sleep 3
"$PYTHON" scripts/spike_bench.py "recovery" direct "$fans" --hold "$hold" --no-record --keep-failed | grep -E 'requests_per_second|failed_percent'
start=$(date +%s)
while :; do
  code=$(curl -s -m 4 -o /dev/null -w '%{http_code}' -H 'Host: localhost' http://127.0.0.1:8001/api/v1/events/LOADTEST/availability)
  now=$(( $(date +%s) - start ))
  [ "$code" = 200 ] && { echo "RECOVERED after $now s, $fans fans for $hold s"; break; }
  [ "$now" -ge "$limit" ] && { echo "NOT RECOVERED after $now s, $fans fans for $hold s"; break; }
  sleep 1
done
