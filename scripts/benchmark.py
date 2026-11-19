# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Measure the running Compose stack and write docs/benchmarks.json. Every published number comes from here.

    python scripts/benchmark.py import 1000000
    python scripts/benchmark.py gate --users 300 --seconds 60

Import results replace earlier results with the same name. This script wipes the local stack's demo tables.
"""

import argparse
import csv
import datetime as dt
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8000/api/v1"
OUT = ROOT / os.environ.get("ENCORE_BENCH_OUT", "docs/benchmarks.json")
HEADERS = {"Authorization": f"Bearer {os.environ.get('ENCORE_OPERATOR_TOKEN', '')}"}


def compose(*args: str) -> str:
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def sql(statement: str) -> str:
    return compose("exec", "-T", "db", "psql", "-U", "encore", "-tAc", statement).strip()


def machine() -> str:
    if os.environ.get("ENCORE_BENCH_MACHINE"):
        return os.environ["ENCORE_BENCH_MACHINE"]
    cores = compose("exec", "-T", "db", "nproc").strip()
    memory = compose("exec", "-T", "db", "sh", "-c", "grep MemTotal /proc/meminfo").split()[1]
    return (
        f"{platform.system()} host, Docker Desktop VM with {cores} CPUs and {int(memory) // 1024 // 1024} GB"
    )


def record(result: dict) -> None:
    results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else []
    results = [row for row in results if row["test"] != result["test"]]
    results.append(result)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps(result, indent=2), flush=True)


class MemoryWatch(threading.Thread):
    """Sample the worker's memory, to show that a bigger file does not need more of it."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak = 0.0
        self.running = True

    def run(self) -> None:
        container = compose("ps", "-q", "worker").strip()
        while self.running:
            out = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            found = re.match(r"([\d.]+)(MiB|GiB)", out)
            if found:
                value = float(found[1]) * (1024 if found[2] == "GiB" else 1)
                self.peak = max(self.peak, value)
            time.sleep(3)


def wait_run(run_id: int, seconds: int) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = httpx.get(f"{API}/imports/runs/{run_id}", timeout=30).json()
        if state["status"] in ("completed", "failed", "interrupted"):
            return state
        time.sleep(2)
    raise SystemExit(f"Run {run_id} did not finish in {seconds}s")


def bench_import(rows: int) -> None:
    name = f"bench_{rows}.csv"
    if not (ROOT / "data" / name).exists():
        print("generating", rows, flush=True)
        compose(
            "exec",
            "-T",
            "worker",
            "python",
            "manage.py",
            "gen_legacy",
            "--rows",
            str(rows),
            "--out",
            f"data/{name}",
        )
    sql("truncate crm_contact, crm_contactalias, imports_importrun cascade")
    # Right after `compose up` the edge can answer before Django does, with a 502.
    for _ in range(60):
        try:
            if httpx.get(f"{API}/imports/runs", timeout=10).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(2)
    watch = MemoryWatch()
    watch.start()
    reply = httpx.post(f"{API}/imports/runs", json={"source": name}, headers=HEADERS, timeout=30)
    reply.raise_for_status()
    state = wait_run(reply.json()["id"], 4 * 3600)
    watch.running = False
    if state["status"] != "completed":
        raise SystemExit(f"Run ended as {state['status']}: {state['error']}")
    started = dt.datetime.fromisoformat(state["started_at"])
    finished = dt.datetime.fromisoformat(state["finished_at"])
    seconds = (finished - started).total_seconds()
    report = state["reconciliation"]
    record(
        {
            "test": f"Import {rows:,} rows",
            "rows": state["rows_read"],
            "seconds_total": round(seconds, 1),
            "rows_per_second": round(state["rows_read"] / seconds),
            "includes": "hashing, import, full reconciliation",
            "file_mb": round(state["source_size"] / 1024 / 1024, 1),
            "worker_peak_mb": round(watch.peak),
            "discrepancies": report["discrepancies"],
            "quarantined": state["rows_quarantined"],
            "machine": machine(),
            "data": "synthetic",
            "measured": dt.date.today().isoformat(),
        }
    )


def bench_gate(users: int, seconds: int, tickets: int) -> None:
    sql(
        "truncate ticketing_ticket, ticketing_outboundcheckin, ticketing_deadletter, ticketing_synccursor cascade"
    )
    # Stop the sync while measuring, so the load is only what is being measured.
    compose("stop", "beat")
    sql(
        "insert into ticketing_ticket (external_id, event_id_external, status, version) "
        f"select 'G' || n, 'LOADTEST', 'valid', 1 from generate_series(0, {tickets - 1}) n"
    )
    with tempfile.TemporaryDirectory() as folder:
        prefix = str(Path(folder) / "gate")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "locust",
                "-f",
                str(ROOT / "scripts" / "locustfile.py"),
                "--headless",
                "-u",
                str(users),
                "-r",
                str(max(1, users // 10)),
                "-t",
                f"{seconds}s",
                "--host",
                "http://127.0.0.1:8000",
                "--csv",
                prefix,
                "--only-summary",
            ],
            cwd=ROOT,
            env={**os.environ, "LOCUST_TICKETS": str(tickets), "PYTHONIOENCODING": "utf-8"},
            check=False,
            capture_output=True,
        )
        with open(f"{prefix}_stats.csv", newline="", encoding="utf-8") as handle:
            rows = {row["Name"]: row for row in csv.DictReader(handle)}
    compose("start", "beat")
    admitted = int(sql("select count(*) from ticketing_ticket where checked_in_at is not null"))
    outbox = int(sql("select count(*) from ticketing_outboundcheckin"))
    for name, row in rows.items():
        record(
            {
                "test": f"Gate spike, {name}",
                "users": users,
                "seconds": seconds,
                "requests": int(row["Request Count"]),
                "failed": int(row["Failure Count"]),
                "requests_per_second": round(float(row["Requests/s"])),
                "p50_ms": row["50%"],
                "p95_ms": row["95%"],
                "p99_ms": row["99%"],
                "tickets_admitted": admitted,
                "outbox_messages": outbox,
                "machine": f"{machine()}; load generator on the same machine",
                "data": "synthetic",
                "measured": dt.date.today().isoformat(),
            }
        )
    if admitted != outbox:
        raise SystemExit(f"Admitted {admitted} tickets but wrote {outbox} outbox messages")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["import", "gate"])
    parser.add_argument("rows", nargs="?", type=int, default=1_000_000)
    parser.add_argument("--users", type=int, default=300)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--tickets", type=int, default=500_000)
    args = parser.parse_args()
    if args.kind == "import":
        bench_import(args.rows)
    else:
        bench_gate(args.users, args.seconds, args.tickets)


if __name__ == "__main__":
    main()
