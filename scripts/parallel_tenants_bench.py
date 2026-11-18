"""How far does running organizers side by side help? Writes docs/benchmarks-parallel.json.

One organizer at a time is safe but slow when thousands are waiting. Here N organizers import the same number of rows
each, with N imports running at once. Needs the stack running: bash scripts/run_benchmarks.sh --stack-only ...
This wipes the local stack's demo tables.
"""

import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8000/api/v1"
HEADERS = {"Authorization": f"Bearer {os.environ.get('ENCORE_OPERATOR_TOKEN', '')}"}
ROWS_EACH = 250_000
OUT = ROOT / "docs" / "benchmarks-parallel.json"


def compose(*args: str) -> str:
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def sql(statement: str) -> str:
    return compose("exec", "-T", "db", "psql", "-U", "encore", "-tAc", statement).strip()


def wipe() -> None:
    sql(
        "truncate imports_importrun, imports_importbatch, imports_quarantinerow, imports_contactchange, "
        "imports_aliaschange, crm_contactalias, crm_contact cascade"
    )


def measure(organizers: int) -> dict:
    wipe()
    started = time.monotonic()
    ids = []
    for index in range(organizers):
        reply = httpx.post(
            f"{API}/imports/runs",
            json={"source": f"parallel_{index}.csv", "organizer": f"organizer-{index}"},
            headers=HEADERS,
            timeout=30,
        )
        reply.raise_for_status()
        ids.append(reply.json()["id"])
    while True:
        runs = [httpx.get(f"{API}/imports/runs/{pk}", timeout=30).json() for pk in ids]
        if all(run["status"] in ("completed", "failed") for run in runs):
            break
        time.sleep(1)
    seconds = time.monotonic() - started
    rows = sum(run["rows_read"] for run in runs)
    return {
        "organizers_at_once": organizers,
        "rows_each": ROWS_EACH,
        "seconds_including_reconciliation": round(seconds, 1),
        "rows_per_second_all_together": round(rows / seconds),
        "statuses": sorted({run["status"] for run in runs}),
        "discrepancies": sum((run["reconciliation"] or {}).get("discrepancies", -1) for run in runs),
        "cross_organizer_merges": int(
            sql(
                "select count(*) from crm_contactalias a join crm_contact c on c.id=a.contact_id where a.organizer<>c.organizer"
            )
        ),
    }


def main() -> None:
    for index in range(4):
        compose(
            "exec",
            "-T",
            "worker",
            "python",
            "manage.py",
            "gen_legacy",
            "--rows",
            str(ROWS_EACH),
            "--out",
            f"data/parallel_{index}.csv",
            "--seed",
            str(100 + index),
        )
    results = []
    for workers, organizers in ((1, 1), (1, 2), (2, 4)):
        compose("up", "-d", "--wait", "--scale", f"worker={workers}", "worker")
        result = measure(organizers)
        result["celery_slots"] = workers * 2
        results.append(result)
        print(result, flush=True)
    compose("up", "-d", "--wait", "--scale", "worker=1", "worker")
    OUT.write_text(
        json.dumps(
            {
                "measured": dt.date.today().isoformat(),
                "machine": "Docker Desktop VM with 8 CPUs and 7 GB; one PostgreSQL; synthetic data; one run per row",
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
