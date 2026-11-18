#!/usr/bin/env bash
# Run all measurements against the stack in production settings (DEBUG off, token on).
# Usage: bash scripts/run_benchmarks.sh [rows ...]   Default: 1000000 10000000
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=python
[ -x .venv/Scripts/python ] && PYTHON=.venv/Scripts/python
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python

ENCORE_SECRET_KEY="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))')"
ENCORE_OPERATOR_TOKEN="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
TICKETING_WEBHOOK_SECRET="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
# One key stands in for thousands of gates and fans here, so its limit is lifted. Every call still pays for the check.
ENCORE_RATE_PER_KEY=100000000/min
export ENCORE_DEBUG=0 ENCORE_SECRET_KEY ENCORE_OPERATOR_TOKEN TICKETING_WEBHOOK_SECRET ENCORE_RATE_PER_KEY PYTHONIOENCODING=utf-8

docker compose up -d --build --wait db redis web edge worker worker-ticketing beat mock-ticketing

if [ "${1:-}" = "--stack-only" ]; then
  shift
  exec "$@"
fi

for rows in "${@:-1000000 10000000}"; do
  for count in $rows; do
    if [ "$count" = 10000000 ]; then
      "$PYTHON" scripts/benchmark.py gate
    fi
    "$PYTHON" scripts/benchmark.py import "$count"
  done
done
