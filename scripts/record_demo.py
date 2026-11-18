"""Record the five minute demo as a video, from the real console against the real stack.

Needs Playwright with Chromium: pip install playwright, then playwright install chromium.
Usage: python scripts/record_demo.py
Wipes the local demo tables, runs the whole scenario, and writes docs/demo.webm and docs/shots/.
Stops with an error if a guarantee does not hold, so a saved video is also a passed run.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8000"
API = f"{URL}/api/v1"
FILE = "demo_contacts.csv"
SHOTS = ROOT / "docs" / "shots"
# ENCORE_DEMO_LANG=uk records the Ukrainian video: translated console and captions, written next to the English one.
UK = os.environ.get("ENCORE_DEMO_LANG") == "uk"
if UK:
    SHOTS = ROOT / "docs" / "shots-uk"
    CAPTIONS_UK = json.loads((ROOT / "scripts" / "pack_uk" / "captions_uk.json").read_text(encoding="utf-8"))

CAPTION_CSS = """
#demo-caption { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 99999;
  background: #14171a; color: #fff; font: 500 20px/1.35 system-ui, sans-serif; padding: 12px 22px;
  border-radius: 10px; max-width: 80%; text-align: center; box-shadow: 0 8px 30px rgba(0,0,0,.3); }
"""


# What the pack's offline replay plays back: real replies from this real run, grouped by demo step.
REPLAY: dict = {"steps": [], "pages": {}}


def begin_step(key: str, title: str, path: str, note: str) -> None:
    """Everything the page fetches from now on belongs to this step of the replay."""
    REPLAY["steps"].append({"key": key, "title": title, "path": path, "note": note, "frames": []})


def record_response(response) -> None:
    url = response.url
    if not url.startswith(URL) or response.status != 200:
        return
    path = url[len(URL) :]
    if response.request.resource_type == "document":
        REPLAY["pages"][path] = response.text()
    elif path.startswith("/api/v1/") and REPLAY["steps"] and response.request.method == "GET":
        try:
            body = response.json()
        except ValueError:
            return
        REPLAY["steps"][-1]["frames"].append(
            {"t": time.time(), "path": path, "body": body, "total": response.headers.get("x-total-count")}
        )


def compose(*args: str) -> str:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        # Restarted services must come back in the same demo settings the stack was started with.
        env={**os.environ, "ENCORE_DEBUG": os.environ.get("ENCORE_DEBUG", "1")},
    ).stdout


def caption(page, text: str, hold: float = 3.0) -> None:
    if UK:
        text = translate_caption(text)
    print(text, flush=True)
    page.evaluate(
        """(text) => {
            let el = document.getElementById('demo-caption');
            if (!el) {
                const style = document.createElement('style');
                style.textContent = window.__captionCss;
                document.head.append(style);
                el = document.createElement('div');
                el.id = 'demo-caption';
                document.body.append(el);
            }
            el.textContent = text;
        }""",
        text,
    )
    page.wait_for_timeout(int(hold * 1000))


def translate_caption(text: str) -> str:
    if text in CAPTIONS_UK:
        return CAPTIONS_UK[text]
    found = re.fullmatch(r"Interrupted\. The last durable checkpoint is row ([\d,]+)\.", text)
    if found:
        return f"Перервано. Остання надійна контрольна точка — рядок {found[1].replace(',', ' ')}."
    raise SystemExit(f"No Ukrainian caption for: {text}")


def run_state(run_id: int) -> dict:
    return httpx.get(f"{API}/imports/runs/{run_id}", timeout=15).json()


def wait_run(run_id: int, check, seconds: int, label: str, page) -> dict:
    deadline = time.time() + seconds
    state = {}
    while time.time() < deadline:
        state = run_state(run_id)
        if check(state):
            return state
        page.wait_for_timeout(500)
    raise SystemExit(f"Timed out waiting for {label}: {state.get('status')}")


def wait_health(check, seconds: int, label: str, page) -> dict:
    deadline = time.time() + seconds
    health = {}
    while time.time() < deadline:
        health = httpx.get(f"{API}/connector/health", timeout=15).json()
        if check(health):
            return health
        page.wait_for_timeout(500)
    raise SystemExit(f"Timed out waiting for {label}: {health.get('breaker')}")


def shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"))


def scenario(page) -> None:
    begin_step("start", "Start an import", "/", "The operator picks a legacy export and an organizer.")
    page.goto(URL)
    page.wait_for_selector("#start-btn:not([disabled])")
    caption(page, "A legacy CRM export: 300,000 rows of synthetic contacts, with broken rows on purpose.", 4)
    page.select_option("#source", FILE)
    page.click("#start-btn")
    page.wait_for_url("**/runs/*", timeout=20_000)
    run_id = int(page.url.rstrip("/").rsplit("/", 1)[1])
    begin_step(
        "running",
        "Import running",
        f"/runs/{run_id}",
        "Each batch commits rows, counters and checkpoint in one transaction.",
    )
    caption(page, "Each batch commits its rows, counters and checkpoint in one transaction.", 5)
    wait_run(run_id, lambda s: s["rows_read"] >= 60_000, 200, "20% progress", page)
    shot(page, "1-running")

    begin_step(
        "killed",
        "Worker killed",
        f"/runs/{run_id}",
        "The worker got SIGKILL mid-import. Numbers stop moving, and the heartbeat goes stale.",
    )
    compose("kill", "-s", "SIGKILL", "worker")
    if run_state(run_id)["status"] != "running":
        raise SystemExit("The kill landed after the import ended")
    caption(page, "The worker was just killed with SIGKILL, in the middle of the import.", 5)
    compose("up", "-d", "worker")
    caption(page, "It is back. The run's heartbeat goes stale, and the reaper will notice.", 2)
    state = wait_run(run_id, lambda s: s["status"] == "interrupted", 120, "reaper", page)
    begin_step(
        "interrupted",
        "Interrupted",
        f"/runs/{run_id}",
        "The reaper marked the run interrupted. Nothing after the last checkpoint counts.",
    )
    page.wait_for_timeout(5000)
    shot(page, "2-interrupted")
    caption(page, f"Interrupted. The last durable checkpoint is row {state['checkpoint_row']:,}.", 5)

    begin_step("resumed", "Resumed", f"/runs/{run_id}", "Resume continues from that exact byte in the file.")
    page.get_by_role("button", name="Продовжити" if UK else "Resume").click()
    caption(page, "Resume continues from that exact byte in the file.", 3)
    state = wait_run(run_id, lambda s: s["status"] in ("completed", "failed"), 400, "completion", page)
    if state["status"] != "completed" or not state["reconciliation"]["balanced"]:
        raise SystemExit(f"Run ended {state['status']} with report {state['reconciliation']}")
    begin_step(
        "completed",
        "Completed and reconciled",
        f"/runs/{run_id}",
        "The file was read again and compared with the database.",
    )
    page.wait_for_timeout(5000)
    shot(page, "3-completed")
    caption(page, "The file was read again and compared with the database: 0 discrepancies.", 7)

    begin_step(
        "quarantine",
        "Quarantine",
        f"/runs/{run_id}/quarantine",
        "Broken rows are kept with their reason, for a person to correct or dismiss.",
    )
    page.goto(f"{URL}/runs/{run_id}/quarantine")
    page.wait_for_timeout(1500)
    shot(page, "4-quarantine")
    caption(
        page,
        "Broken rows are never dropped. Each is kept with its reason, for a person to correct or dismiss.",
        6,
    )

    begin_step(
        "healthy",
        "Ticketing connector",
        "/integration",
        "A stand-in for the external API, with failures that can be switched on.",
    )
    page.goto(f"{URL}/integration")
    page.wait_for_timeout(1500)
    caption(page, "Now the ticketing API. This is a stand-in, with failures we can switch on.", 5)
    begin_step(
        "failures",
        "API failing",
        "/integration",
        "30% server errors and 20% rate limits during a full re-sync. The connector retries with backoff.",
    )
    httpx.post(
        f"{API}/connector/faults", json={"p_500": 0.3, "p_429": 0.2, "retry_after": 0.2}
    ).raise_for_status()
    page.wait_for_timeout(1500)
    compose(
        "exec", "-T", "db", "psql", "-U", "encore", "-c", "truncate ticketing_ticket, ticketing_synccursor"
    )
    caption(
        page,
        "30% server errors and 20% rate limits, during a full re-sync. The connector retries with backoff.",
        25,
    )
    shot(page, "5-integration-failures")

    begin_step(
        "outage",
        "Full outage",
        "/integration",
        "The circuit breaker opens. Calls are refused locally, so the service is left alone.",
    )
    httpx.post(f"{API}/connector/faults", json={"outage": True}).raise_for_status()
    caption(page, "A full outage.", 2)
    wait_health(lambda h: h["breaker"] == "open", 90, "breaker to open", page)
    page.wait_for_timeout(2500)
    shot(page, "6-integration-open")
    caption(page, "The circuit breaker opens. Calls are refused locally, so the service is left alone.", 7)
    begin_step(
        "recovered",
        "Recovered",
        "/integration",
        "The breaker probes and closes by itself. Sync catches up with nothing lost.",
    )
    httpx.post(f"{API}/connector/faults", json={}).raise_for_status()
    caption(page, "The service is back. The breaker probes and closes by itself.", 2)
    wait_health(lambda h: h["breaker"] == "closed" and h["caught_up"], 200, "recovery", page)
    page.wait_for_timeout(2500)
    shot(page, "7-integration-recovered")
    caption(page, "Recovered, and caught up, with nothing lost.", 5)

    begin_step(
        "load",
        "Load results",
        "/load-results",
        "Measurements from the results file, on the machine each one names.",
    )
    page.goto(f"{URL}/load-results")
    page.wait_for_timeout(1500)
    shot(page, "8-load-results")
    caption(
        page,
        "Measured on a laptop, synthetic data: 10 million rows imported, 20,000 fans refreshing, 0 failed.",
        7,
    )


def main() -> None:
    compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "encore",
        "-c",
        "truncate crm_contact, crm_contactalias, imports_importrun cascade",
    )
    httpx.post(f"{API}/connector/faults", json={}, timeout=10).raise_for_status()
    with tempfile.TemporaryDirectory() as folder, sync_playwright() as play:
        browser = play.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=folder,
            record_video_size={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.add_init_script(f"window.__captionCss = {CAPTION_CSS!r};")
        if UK:
            page.add_init_script((ROOT / "scripts" / "pack_uk" / "console_uk.js").read_text(encoding="utf-8"))
        page.on("response", record_response)
        try:
            scenario(page)
        finally:
            video = page.video.path() if page.video else None
            context.close()
            browser.close()
        target = ROOT / "docs" / ("demo-uk.webm" if UK else "demo.webm")
        if video:
            shutil.copy(video, target)
    if not UK:
        # Static pages and every API reply seen, for scripts/build_pack.py. Generated, like the video.
        (ROOT / "docs" / "replay.json").write_text(json.dumps(REPLAY), encoding="utf-8", newline="\n")
    print(f"saved {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
