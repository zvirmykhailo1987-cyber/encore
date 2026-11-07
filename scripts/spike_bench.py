"""Measure refresh spikes with k6 and add the results to docs/benchmarks.json.

Needs the stack running with production settings: bash scripts/run_benchmarks.sh --stack-only
Usage: python scripts/spike_bench.py <name> <target> <fans> [--scans N] [--hold S] [--think S] [--kill-web S]
Target is "edge" or "direct". This wipes the ticket tables of the local stack.
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / os.environ.get("ENCORE_BENCH_OUT", "docs/benchmarks.json")
TARGETS = {"edge": "http://edge", "direct": "http://web:8000"}
TICKETS = 500_000


def compose(*args: str) -> str:
    done = subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True)
    if done.returncode:
        raise SystemExit(f"docker compose {' '.join(args)} failed: {done.stderr[-800:]}")
    return done.stdout


def sql(statement: str) -> str:
    return compose("exec", "-T", "db", "psql", "-U", "encore", "-tAc", statement).strip()


def seed() -> None:
    sql(
        "truncate ticketing_ticket, ticketing_outboundcheckin, ticketing_deadletter, ticketing_synccursor cascade"
    )
    sql(
        "insert into ticketing_ticket (external_id, event_id_external, status, version) "
        f"select 'G' || n, 'LOADTEST', 'valid', 1 from generate_series(0, {TICKETS - 1}) n"
    )
    sql("analyze ticketing_ticket")


def settled_counts() -> tuple[int, int]:
    """Count once Django has finished requests the load generator already gave up on."""
    last = (-1, -1)
    for _ in range(60):
        admitted = int(sql("select count(*) from ticketing_ticket where checked_in_at is not null"))
        outbox = int(sql("select count(*) from ticketing_outboundcheckin"))
        if (admitted, outbox) == last:
            break
        last = (admitted, outbox)
        time.sleep(2)
    return last


def machine() -> str:
    if os.environ.get("ENCORE_BENCH_MACHINE"):
        return os.environ["ENCORE_BENCH_MACHINE"]
    cores = compose("exec", "-T", "db", "nproc").strip()
    memory = compose("exec", "-T", "db", "sh", "-c", "grep MemTotal /proc/meminfo").split()[1]
    return f"Docker Desktop VM with {cores} CPUs and {int(memory) // 1024 // 1024} GB; load generator in the same VM"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("target", choices=TARGETS)
    parser.add_argument("fans", type=int)
    parser.add_argument("--scans", type=int, default=0)
    parser.add_argument("--hold", type=int, default=60)
    parser.add_argument("--think", type=float, default=2)
    parser.add_argument(
        "--kill-web", type=int, default=0, help="Stop Django this many seconds after the start"
    )
    parser.add_argument("--browser", action="store_true", help="Ask for compressed replies, as browsers do")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--keep-failed", action="store_true", help="Record a run even if many requests failed")
    args = parser.parse_args()

    compose("stop", "beat")
    seed()
    if args.kill_web:

        def kill() -> None:
            time.sleep(args.kill_web)
            compose("stop", "web")
            print("web stopped", flush=True)

        threading.Thread(target=kill, daemon=True).start()
    command = [
        "run",
        "--rm",
        "-T",
        "-e",
        f"TARGET={TARGETS[args.target]}",
        "-e",
        f"FANS={args.fans}",
        "-e",
        f"THINK={args.think}",
        "-e",
        f"HOLD={args.hold}",
        "-e",
        f"SCANS={args.scans}",
        "-e",
        f"TICKETS={TICKETS}",
        "-e",
        f"BROWSER={int(args.browser)}",
        "k6",
        "run",
        "--quiet",
        "--no-usage-report",
        "/scripts/spike.js",
    ]
    result = subprocess.run(
        ["docker", "compose", "--profile", "load", *command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise SystemExit(f"k6 produced no summary. exit {result.returncode}\n{result.stderr[-1500:]}")
    summary = json.loads(lines[-1])
    if args.kill_web:
        compose("start", "web")
    compose("start", "beat")

    admitted, outbox = settled_counts()
    record = {
        "test": args.name,
        "through": "edge cache" if args.target == "edge" else "Django directly",
        "fans": args.fans,
        "seconds_between_refreshes": args.think,
        "seconds_at_peak": args.hold,
        "scans_per_second_requested": args.scans,
        "requests": summary["requests"],
        "requests_per_second": summary["requests_per_second"],
        "failed_percent": round(summary["failed_rate"] * 100, 3),
        "unexpected_responses": summary["unexpected_responses"],
        "served_from_cache": summary["edge_cache_hits"],
        "fan_p50_ms": round(summary["fan"]["p50"], 1),
        "fan_p95_ms": round(summary["fan"]["p95"], 1),
        "fan_p99_ms": round(summary["fan"]["p99"], 1),
        "tickets_admitted": admitted,
        "outbox_messages": outbox,
        "machine": machine(),
        "data": "synthetic",
        "measured": dt.date.today().isoformat(),
    }
    if args.scans:
        record.update(
            gate_p50_ms=round(summary["gate"]["p50"], 1),
            gate_p95_ms=round(summary["gate"]["p95"], 1),
            gate_p99_ms=round(summary["gate"]["p99"], 1),
            gate_failed_percent=round((summary["gate"]["failed_rate"] or 0) * 100, 3),
        )
    print(json.dumps(record, indent=1), flush=True)
    if record["failed_percent"] > 5 and not args.kill_web and not args.keep_failed:
        raise SystemExit(f"{record['failed_percent']}% of requests failed: not recording this as a result")
    if not args.no_record:
        results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else []
        results = [row for row in results if row["test"] != args.name]
        results.append(record)
        OUT.write_text(json.dumps(results, indent=2), encoding="utf-8", newline="\n")
    if admitted != outbox:
        raise SystemExit(f"Admitted {admitted} tickets but wrote {outbox} outbox messages")


if __name__ == "__main__":
    main()
