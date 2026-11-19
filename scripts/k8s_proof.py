# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Run the Kubernetes manifests on a real single-node cluster (k3s in Docker) and write docs/k8s-proof.json.

It deploys everything, kills the import worker mid-run, restarts Django under traffic, and tries to reach the database
from a pod the network policy should stop. Needs Docker. Removes its own cluster container at the end.
"""

import datetime as dt
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "k8s-proof.json"
CLUSTER = "encore-k3s"
K3S_IMAGE = "rancher/k3s:v1.31.6-k3s1"
ROWS = 300_000
TOKEN = secrets.token_urlsafe(32)


def run(*args: str, stdin: bytes | None = None, check: bool = True) -> str:
    done = subprocess.run(args, input=stdin, capture_output=True, cwd=ROOT)
    if check and done.returncode:
        raise SystemExit(f"{' '.join(args)[:300]} failed: {done.stderr.decode(errors='replace')[-1500:]}")
    return done.stdout.decode(errors="replace")


def kubectl(*args: str, check: bool = True) -> str:
    return run("docker", "exec", CLUSTER, "kubectl", "-n", "encore", *args, check=check)


def pod_python(deployment: str, code: str, check: bool = True) -> str:
    return kubectl("exec", f"deploy/{deployment}", "--", "python", "-c", code, check=check)


def api(method: str, path: str, body: dict | None = None) -> dict:
    code = f"""
import httpx, json
r = httpx.request({method!r}, "http://edge/api/v1{path}", json={body!r}, timeout=30,
                  headers={{"Authorization": "Bearer {TOKEN}", "Host": "edge"}})
print(json.dumps({{"status": r.status_code, "body": r.json()}}))
"""
    return json.loads(pod_python("web", code))


def wait_for(condition, timeout: float, what: str, every: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(every)
    raise SystemExit(f"Timed out waiting for: {what}")


def main() -> None:
    proof: dict = {
        "measured": dt.date.today().isoformat(),
        "cluster": f"Single-node k3s ({K3S_IMAGE}) in Docker Desktop",
    }
    run("docker", "rm", "-f", CLUSTER, check=False)
    print("building image", flush=True)
    run("docker", "build", "-t", "encore:local", ".")
    # The volume keeps the cluster's own images, so a second run does not download them again.
    run(
        "docker",
        "run",
        "-d",
        "--privileged",
        "--name",
        CLUSTER,
        "-v",
        "encore-k3s-images:/var/lib/rancher/k3s/agent/containerd",
        K3S_IMAGE,
        "server",
        "--disable=traefik",
        "--disable=metrics-server",
    )
    try:
        wait_for(
            lambda: "Ready" in run("docker", "exec", CLUSTER, "kubectl", "get", "nodes", check=False),
            180,
            "node",
        )
        for image in ("encore:local", "postgres:17-alpine", "redis:7-alpine", "nginx:1.27-alpine"):
            print("loading", image, flush=True)
            tar = subprocess.run(["docker", "save", image], capture_output=True, check=True).stdout
            run("docker", "exec", "-i", CLUSTER, "ctr", "images", "import", "-", stdin=tar)
        print("waiting for the cluster's DNS and storage", flush=True)
        for label in ("k8s-app=kube-dns", "app=local-path-provisioner"):
            wait_for(
                lambda selector=label: (
                    run(
                        "docker",
                        "exec",
                        CLUSTER,
                        "kubectl",
                        "-n",
                        "kube-system",
                        "get",
                        "pods",
                        "-l",
                        selector,
                        "-o",
                        "jsonpath={.items[0].status.containerStatuses[0].ready}",
                        check=False,
                    ).strip()
                    == "true"
                ),
                2400,
                label,
                5,
            )
        run("docker", "cp", "deploy/k8s", f"{CLUSTER}:/manifests")
        run("docker", "exec", CLUSTER, "kubectl", "create", "namespace", "encore", check=False)
        kubectl(
            "create",
            "secret",
            "generic",
            "encore-secrets",
            f"--from-literal=ENCORE_SECRET_KEY={secrets.token_urlsafe(48)}",
            f"--from-literal=ENCORE_OPERATOR_TOKEN={TOKEN}",
            f"--from-literal=TICKETING_WEBHOOK_SECRET={secrets.token_urlsafe(32)}",
            f"--from-literal=POSTGRES_PASSWORD={secrets.token_hex(16)}",
        )
        started = time.monotonic()
        run("docker", "exec", CLUSTER, "kubectl", "apply", "-k", "/manifests")
        kubectl("wait", "--for=condition=complete", "job/migrate", "--timeout=420s")
        for name in ("web", "worker", "worker-ticketing", "beat", "edge"):
            kubectl("rollout", "status", f"deploy/{name}", "--timeout=240s")
        proof["deploy_seconds"] = round(time.monotonic() - started)
        proof["pods"] = kubectl(
            "get",
            "pods",
            "--no-headers",
            "-o",
            "custom-columns=NAME:.metadata.labels.app,READY:.status.phase",
        ).split("\n")
        print("deployed in", proof["deploy_seconds"], flush=True)

        # Metrics reach Prometheus but not the public, and the policy stops pods that have no business there.
        metrics_inside = kubectl("exec", "deploy/edge", "--", "wget", "-qO-", "http://web:8000/metrics")
        public = kubectl(
            "exec",
            "deploy/edge",
            "--",
            "sh",
            "-c",
            "wget -S -qO- http://127.0.0.1/metrics 2>&1 | head -1",
        )
        proof["metrics_from_inside"] = "encore_outbox_pending" in metrics_inside
        proof["metrics_through_public_edge"] = public.strip()
        probe = """
import socket
results = {}
for host, port in (("db", 5432), ("redis", 6379), ("web", 8000)):
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect((host, port)); results[host] = "reachable"
    except OSError as exc:
        results[host] = "blocked"
print(results)
"""
        proof["reachable_from_unrelated_pod"] = pod_python("mock-ticketing", probe).strip()
        proof["reachable_from_web_pod"] = pod_python(
            "web", probe.replace('("web", 8000)', '("edge", 80)')
        ).strip()

        # An import, with its worker killed in the middle.
        pod_python(
            "worker",
            f"import subprocess; subprocess.run('python manage.py gen_legacy --rows {ROWS} --out data/k8s.csv', shell=True, check=True, stdout=subprocess.DEVNULL)",
        )
        reply = api("POST", "/imports/runs", {"source": "k8s.csv", "organizer": "k8s-demo"})
        run_id = reply["body"]["id"]
        wait_for(
            lambda: (api("GET", f"/imports/runs/{run_id}")["body"].get("progress") or 0) > 0.2,
            300,
            "20% progress",
        )
        before = api("GET", f"/imports/runs/{run_id}")["body"]
        killed = time.monotonic()
        kubectl("delete", "pod", "-l", "app=worker", "--grace-period=0", "--force", check=False)
        print("worker killed at row", before["checkpoint_row"], flush=True)
        finished = wait_for(
            lambda: (lambda r: r if r["status"] == "completed" else None)(
                api("GET", f"/imports/runs/{run_id}")["body"]
            ),
            900,
            "import to finish",
            5,
        )
        report = finished["reconciliation"]
        proof["worker_killed"] = {
            "rows_in_file": ROWS,
            "checkpoint_row_when_killed": before["checkpoint_row"],
            "seconds_until_finished_after_kill": round(time.monotonic() - killed),
            "resume_count": finished["resume_count"],
            "resumed_by": "reaper, automatically; no person pressed anything",
            "rows_read": finished["rows_read"],
            "discrepancies": report["discrepancies"],
            "balanced": report["balanced"],
        }
        print(proof["worker_killed"], flush=True)

        # Django restarted while requests keep arriving through the edge.
        traffic = f"""
import httpx, json, threading, time
stop = time.time() + 75
counts = {{}}
lock = threading.Lock()
def loop():
    with httpx.Client(timeout=10, headers={{"Host": "edge", "Authorization": "Bearer {TOKEN}"}}) as c:
        while time.time() < stop:
            try:
                status = c.get("http://edge/api/v1/imports/runs").status_code
            except httpx.HTTPError as exc:
                status = type(exc).__name__
            with lock:
                counts[str(status)] = counts.get(str(status), 0) + 1
            time.sleep(0.05)
threads = [threading.Thread(target=loop) for _ in range(8)]
[t.start() for t in threads]; [t.join() for t in threads]
print(json.dumps(counts))
"""
        load = subprocess.Popen(
            [
                "docker",
                "exec",
                CLUSTER,
                "kubectl",
                "-n",
                "encore",
                "exec",
                "deploy/mock-ticketing",
                "--",
                "python",
                "-c",
                traffic,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(8)
        restart_started = time.monotonic()
        kubectl("rollout", "restart", "deploy/web")
        kubectl("rollout", "status", "deploy/web", "--timeout=180s")
        restart_seconds = round(time.monotonic() - restart_started)
        out, err = load.communicate(timeout=180)
        counts = json.loads(out.decode() or "{}")
        total = sum(counts.values())
        proof["rolling_restart_under_traffic"] = {
            "rollout_seconds": restart_seconds,
            "requests": total,
            "replies": counts,
            "failed": total - counts.get("200", 0),
        }
        print(proof["rolling_restart_under_traffic"], err.decode()[-300:], flush=True)
        proof["workers_after_kill"] = kubectl(
            "get", "deploy", "worker", "-o", "jsonpath={.status.readyReplicas}"
        )
    finally:
        OUT.write_text(json.dumps(proof, indent=2), encoding="utf-8", newline="\n")
        print(json.dumps(proof, indent=1), flush=True)
        if "--keep" not in sys.argv:
            run("docker", "rm", "-f", CLUSTER, check=False)


if __name__ == "__main__":
    main()
