"""Gunicorn settings shared by every way of running the stack."""

import gunicorn.workers.gthread

# Gunicorn 26 holds a worker thread for up to this many seconds on a new connection that has sent nothing.
# At its default of 5, with four threads, 64 idle connections blocked a real request for 80 seconds, and a
# flood left Django unresponsive for 4 minutes after it ended. After this wait the connection moves to the
# poller and holds no thread. Do not use 0: in a measurement that failed 49% of gate scans.
# Gunicorn has no setting for this, so tests/test_gunicorn_config.py fails if the constant moves.
gunicorn.workers.gthread.DEFAULT_WORKER_DATA_TIMEOUT = 0.02
