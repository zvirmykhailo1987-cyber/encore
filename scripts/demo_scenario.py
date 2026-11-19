# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""The five minute demo, as a script: kill the worker during an import, resume, then break the ticketing API.

Run it against the Compose stack: python scripts/demo_scenario.py [--rows 300000]
It prints only what the running system reports. It wipes the ticketing tables of the local stack.
"""

import argparse
import json
import subprocess
import time

import httpx

API = "http://127.0.0.1:8000/api/v1"
MOCK = "http://127.0.0.1:8099"
FILE = "demo_contacts.csv"


def compose(*args: str) -> str:
    done = subprocess.run(["docker", "compose", *args], capture_output=True, text=True, check=True)  # noqa: S603, S607
    return done.stdout


def run_state(run_id: int) -> dict:
    return httpx.get(f"{API}/imports/runs/{run_id}", timeout=10).json()


def wait_for(run_id: int, check, seconds: float, label: str) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = run_state(run_id)
        if check(state):
            return state
        time.sleep(0.25)
    raise SystemExit(f"Timed out waiting for: {label}. Last state: {json.dumps(state)[:600]}")


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def counters(state: dict) -> dict:
    keys = ("rows_read", "rows_created", "rows_merged", "rows_updated", "rows_skipped", "rows_quarantined")
    return {key.removeprefix("rows_"): state[key] for key in keys}


def import_part(rows: int) -> None:
    step(f"Generate {rows:,} synthetic legacy rows")
    print(
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
            f"data/{FILE}",
        )
    )

    step("Start the import")
    started = time.time()
    run = httpx.post(f"{API}/imports/runs", json={"source": FILE}, timeout=10).json()
    run_id = run["id"]
    print("run", run_id)
    state = wait_for(run_id, lambda s: s["rows_read"] >= rows * 0.2, 300, "20% progress")

    step("Kill the worker with SIGKILL, mid-import")
    compose("kill", "-s", "SIGKILL", "worker")
    state = run_state(run_id)
    print("status right after the kill:", state["status"], counters(state))
    if state["status"] != "running":
        raise SystemExit("The import ended before the kill landed. Use more rows.")

    step("Restart the worker. The reaper marks the run interrupted once its heartbeat is stale")
    compose("up", "-d", "worker")
    state = wait_for(run_id, lambda s: s["status"] == "interrupted", 120, "reaper")
    print("status:", state["status"], "checkpoint row:", state["checkpoint_row"])

    step("Resume")
    resumed = time.time()
    httpx.post(f"{API}/imports/runs/{run_id}/resume", timeout=10).raise_for_status()
    state = wait_for(run_id, lambda s: s["status"] in ("completed", "failed"), 900, "completion")
    print("status:", state["status"], counters(state))
    print("resumes:", state["resume_count"], "unmapped columns:", state["unmapped_columns"])
    print("reconciliation:", json.dumps(state["reconciliation"]))
    print(f"wall clock from start {time.time() - started:.1f}s, after resume {time.time() - resumed:.1f}s")
    if not state["reconciliation"] or not state["reconciliation"]["balanced"]:
        raise SystemExit("RECONCILIATION DID NOT BALANCE")

    step("Run the same file again: it must create nothing")
    again = httpx.post(f"{API}/imports/runs", json={"source": FILE}, timeout=10).json()["id"]
    state = wait_for(again, lambda s: s["status"] in ("completed", "failed"), 900, "second run")
    print("status:", state["status"], counters(state))
    if state["rows_created"] or state["rows_merged"] or state["rows_updated"]:
        raise SystemExit("THE SECOND RUN WROTE DATA")


def connector_part() -> None:
    step("Switch on failures in the ticketing API: 429, 500, duplicates, reordering, malformed items")
    httpx.post(f"{MOCK}/_reset").raise_for_status()
    httpx.post(
        f"{MOCK}/_faults",
        json={
            "p_429": 0.1,
            "p_500": 0.1,
            "p_duplicate": 0.2,
            "p_shuffle": 0.2,
            "p_malformed": 0.1,
            "retry_after": 0.1,
        },
    ).raise_for_status()
    # Beat may have synced everything already. Start over, so the sync really runs through the failures.
    compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "encore",
        "-c",
        "truncate ticketing_ticket, ticketing_synccursor, ticketing_deadletter",
    )
    feed = httpx.get(f"{MOCK}/_stats").json()
    deadline = time.time() + 240
    while time.time() < deadline:
        health = httpx.get(f"{API}/connector/health", timeout=10).json()
        if health["caught_up"]:
            break
        time.sleep(1)
    else:
        raise SystemExit(f"Sync did not catch up: {health}")
    print(
        {
            key: health[key]
            for key in ("breaker", "pages_synced", "items_applied", "items_stale", "dead_letters_open")
        }
    )
    stats = httpx.get(f"{MOCK}/_stats").json()
    print("injected by the mock API:", stats["injected"], "feed size:", feed["feed_size"])
    tickets = compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "encore",
        "-tAc",
        "select count(*), count(*) filter (where version=2) from ticketing_ticket",
    ).strip()
    print("tickets stored, of them on version 2:", tickets)
    if not sum(stats["injected"].values()):
        raise SystemExit("NO FAULT WAS INJECTED: this step proved nothing")
    if tickets != f"{stats['tickets']}|{stats['feed_size'] - stats['tickets']}":
        raise SystemExit("STORED TICKETS DO NOT MATCH THE FEED")

    step("Full outage: the breaker opens, then recovers")
    httpx.post(f"{MOCK}/_faults", json={"outage": True}).raise_for_status()
    # The sync runs every 10 seconds and retries for up to 30 before it counts as failed, so allow two minutes.
    print("breaker during outage:", wait_breaker("open", 120))
    httpx.post(f"{MOCK}/_faults", json={}).raise_for_status()
    print("breaker after recovery:", wait_breaker("closed", 120))


def wait_breaker(wanted: str, seconds: int) -> str:
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = httpx.get(f"{API}/connector/health", timeout=10).json()["breaker"]
        if state == wanted:
            return state
        time.sleep(2)
    raise SystemExit(f"The breaker did not become {wanted} within {seconds} seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=300_000)
    parser.add_argument("--skip-import", action="store_true")
    args = parser.parse_args()
    if not args.skip_import:
        import_part(args.rows)
    connector_part()


if __name__ == "__main__":
    main()
