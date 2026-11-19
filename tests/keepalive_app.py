# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""A tiny WSGI app for driving a real Gunicorn in tests. It reads part of the body, like many views do."""


def app(environ, start_response):
    environ["wsgi.input"].read(4)
    body = b"ok"
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
    return [body]
