"""Gunicorn settings shared by every way of running the stack."""

import time

import gunicorn.workers.gthread

# Longer than the edge's upstream keepalive_timeout (deploy/nginx.conf). Gunicorn's default of 2 seconds let
# Django close a connection that nginx then reused, and nginx does not retry a failed POST.
keepalive = 75

# Gunicorn 26 holds a worker thread for up to this many seconds on a new connection that has sent nothing.
# At its default of 5, with four threads, 64 idle connections blocked a real request for 80 seconds, and a
# flood left Django unresponsive for 4 minutes after it ended. After this wait the connection moves to the
# poller and holds no thread. Gunicorn has no setting for this, so tests/test_gunicorn_config.py fails if
# the constant moves.
gunicorn.workers.gthread.DEFAULT_WORKER_DATA_TIMEOUT = 0.02

# Gunicorn 26 also uses that same constant as the deadline for draining a request body before keeping the
# connection alive. A short first-byte wait therefore also meant: give up keep-alive whenever draining takes
# longer than 20 ms. At 0 every connection was closed after one request, which is what failed 49% of scans
# in an earlier measurement, and under load a few slow drains closed connections too. The drain gets its own
# deadline here.
BODY_DRAIN_SECONDS = 2.0


def _keepalive_after(self, conn, keepalive_requested):
    if not keepalive_requested:
        return False
    return bool(conn.parser.finish_body(deadline=time.monotonic() + BODY_DRAIN_SECONDS))


gunicorn.workers.gthread.ThreadWorker._keepalive_after = _keepalive_after
