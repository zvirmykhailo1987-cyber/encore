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
