# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

import logging
import re
import time
import uuid
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from .metrics import healthz, livez, metrics

logger = logging.getLogger("encore.request")
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

# Style attributes carry the board's layout variables, so inline styles stay allowed. Scripts do not.
POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


class OperationalEndpoints:
    """Probes and scrapes arrive with a pod IP as the host, so they are answered before the host check."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.handlers = {"/livez": livez, "/healthz": healthz, "/metrics": metrics}

    def __call__(self, request: HttpRequest) -> HttpResponse:
        handler = self.handlers.get(request.path)
        return handler(request) if handler else self.get_response(request)


class ContentSecurityPolicy:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not settings.DEBUG:
            response.headers.setdefault("Content-Security-Policy", POLICY)
        return response


class RequestLog:
    """Give every reply a request id, and log the requests slow enough to be worth reading."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        started = time.monotonic()
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if SAFE_REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        response = self.get_response(request)
        response.headers["X-Request-ID"] = request_id
        milliseconds = (time.monotonic() - started) * 1000
        if milliseconds >= settings.ENCORE_SLOW_REQUEST_MS or response.status_code >= 500:
            logger.info(
                "slow or failed request",
                extra={
                    "fields": {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.path,
                        "status": response.status_code,
                        "milliseconds": round(milliseconds, 1),
                    }
                },
            )
        return response
