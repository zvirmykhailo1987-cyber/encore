import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn

os.environ.setdefault("ENCORE_DEBUG", "1")


@pytest.fixture(scope="session")
def mock_server():
    os.environ["MOCK_TICKETS"] = "1000"
    from mock_api.app import app, state

    state.reset()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{url}/_stats", timeout=1)
            break
        except httpx.TransportError:
            time.sleep(0.05)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def mock_api(mock_server):
    httpx.post(f"{mock_server}/_reset")
    return mock_server


@pytest.fixture
def platform_client(settings):
    """An API client holding the platform key. The API is closed to callers without one."""
    import secrets

    from rest_framework.test import APIClient

    settings.ENCORE_OPERATOR_TOKEN = secrets.token_urlsafe(24)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {settings.ENCORE_OPERATOR_TOKEN}")
    return api


@pytest.fixture(autouse=True)
def generous_rate_limits(monkeypatch):
    """Tests share one Redis, so limits would carry over between tests. The limit test sets its own."""
    from rest_framework.throttling import SimpleRateThrottle

    monkeypatch.setattr(
        SimpleRateThrottle,
        "THROTTLE_RATES",
        {"key": "100000/min", "gate-key": "100000/min", "no-key": "100000/min"},
    )


@pytest.fixture(autouse=True)
def import_folder(settings, tmp_path):
    """Each test's files live in its own folder, and the importer only reads files inside the import folder."""
    settings.ENCORE_IMPORT_SOURCE_DIR = tmp_path
