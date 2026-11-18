import inspect
import os
import runpy
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "deploy" / "gunicorn.conf.py"
linux_only = pytest.mark.skipif(sys.platform == "win32", reason="Gunicorn does not run on Windows")


@linux_only
def test_idle_connections_do_not_hold_worker_threads():
    gthread = pytest.importorskip("gunicorn.workers.gthread")
    assert hasattr(gthread, "DEFAULT_WORKER_DATA_TIMEOUT"), "Gunicorn changed: review deploy/gunicorn.conf.py"
    original = inspect.signature(gthread.ThreadWorker._keepalive_after)
    assert list(original.parameters) == ["self", "conn", "keepalive"], (
        "Gunicorn changed: review the drain patch"
    )
    settings = runpy.run_path(str(CONFIG))
    assert 0 < gthread.DEFAULT_WORKER_DATA_TIMEOUT <= 0.1
    assert settings["keepalive"] > 50, (
        "Must outlive the edge's upstream keepalive_timeout in deploy/nginx.conf"
    )


def edge_keepalive_seconds() -> int:
    for line in (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("keepalive_timeout") and line.startswith("        "):
            return int(line.split()[1].rstrip("s;"))
    raise AssertionError("No upstream keepalive_timeout in deploy/nginx.conf")


def test_the_edge_closes_idle_upstream_connections_before_django_does():
    settings = runpy.run_path(str(CONFIG)) if sys.platform != "win32" else {"keepalive": 75}
    assert edge_keepalive_seconds() < settings["keepalive"]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(params=["as deployed", "first-byte wait of 0"])
def gunicorn_server(request, tmp_path):
    config = CONFIG
    if request.param == "first-byte wait of 0":
        # The setting that once failed half of all scans. Keep-alive must not depend on it any more.
        config = tmp_path / "zero.conf.py"
        config.write_text(
            f"exec(open({str(CONFIG)!r}).read())\ngunicorn.workers.gthread.DEFAULT_WORKER_DATA_TIMEOUT = 0\n"
        )
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "tests.keepalive_app:app", "--config", str(config)]
        + ["--bind", f"127.0.0.1:{port}", "--workers", "1", "--threads", "2", "--worker-class", "gthread"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield port
    process.terminate()
    process.wait(timeout=10)


def post(conn: socket.socket, body: bytes) -> bytes:
    head = f"POST /api/v1/scans HTTP/1.1\r\nHost: t\r\nContent-Length: {len(body)}\r\n\r\n".encode()
    conn.sendall(head + body)
    reply = b""
    while b"\r\n\r\n" not in reply or not reply.endswith(b"ok"):
        chunk = conn.recv(4096)
        if not chunk:
            raise ConnectionError("Django closed a keep-alive connection")
        reply += chunk
    return reply.split(b"\r\n", 1)[0]


@linux_only
def test_one_connection_serves_requests_with_bodies_and_survives_an_idle_pause(gunicorn_server):
    with socket.create_connection(("127.0.0.1", gunicorn_server), timeout=5) as conn:
        # A body bigger than the app reads, so Gunicorn must drain the rest before reusing the socket.
        for _ in range(20):
            assert post(conn, b'{"ticket": "T1", "scan_id": "' + b"x" * 2000 + b'"}') == b"HTTP/1.1 200 OK"
        time.sleep(3)  # longer than Gunicorn's old default keep-alive of 2 seconds
        assert post(conn, b"{}") == b"HTTP/1.1 200 OK"
