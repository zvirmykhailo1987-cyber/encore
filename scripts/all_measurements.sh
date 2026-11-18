#!/usr/bin/env bash
# Every published measurement, on the current code, in one go. About two hours on a laptop.
# Usage: bash scripts/all_measurements.sh
set -uo pipefail
export MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.."

PYTHON=python
[ -x .venv/Scripts/python ] && PYTHON=.venv/Scripts/python
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python

full() {
  echo "##### full stack: $*"
  bash scripts/run_benchmarks.sh --stack-only "$@"
  echo "##### exit $?"
}

full bash scripts/full_tier_runs.sh
full "$PYTHON" scripts/parallel_tenants_bench.py
full "$PYTHON" scripts/benchmark.py import 1000000
full "$PYTHON" scripts/benchmark.py import 10000000
full "$PYTHON" scripts/query_plans.py

echo "##### small machine"
export COMPOSE_PATH_SEPARATOR=";" COMPOSE_FILE="docker-compose.yml;docker-compose.low.yml"
bash scripts/run_benchmarks.sh --stack-only bash scripts/low_tier_runs.sh
echo "##### exit $?"
unset COMPOSE_PATH_SEPARATOR COMPOSE_FILE
echo "##### all done"
