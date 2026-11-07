import runpy
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="Gunicorn does not run on Windows")
def test_idle_connections_do_not_hold_worker_threads():
    gthread = pytest.importorskip("gunicorn.workers.gthread")
    assert hasattr(gthread, "DEFAULT_WORKER_DATA_TIMEOUT"), "Gunicorn changed: review deploy/gunicorn.conf.py"
    runpy.run_path(str(Path(__file__).resolve().parent.parent / "deploy" / "gunicorn.conf.py"))
    assert 0 < gthread.DEFAULT_WORKER_DATA_TIMEOUT <= 0.1
