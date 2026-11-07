"""Doors open: gates scan tickets while everyone else refreshes the event page."""

import itertools
import os
import random
import uuid

from locust import HttpUser, between, task

TICKETS = int(os.environ.get("LOCUST_TICKETS", "500000"))
HEADERS = {"Authorization": f"Bearer {os.environ.get('ENCORE_OPERATOR_TOKEN', '')}"}
_next_ticket = itertools.count()


class Gate(HttpUser):
    """A gate device. Sometimes it never got the reply and repeats the same scan."""

    weight = 1
    wait_time = between(0.05, 0.2)
    last: dict | None = None

    @task
    def scan(self) -> None:
        if self.last and random.random() < 0.05:  # noqa: S311
            body = self.last
        else:
            body = {"ticket": f"G{next(_next_ticket) % TICKETS}", "scan_id": str(uuid.uuid4())}
        with self.client.post(
            "/api/v1/scans", json=body, headers=HEADERS, catch_response=True, name="scan"
        ) as reply:
            if reply.status_code == 200 and reply.json()["outcome"] == "admitted":
                reply.success()
            else:
                reply.failure(f"{reply.status_code} {reply.text[:80]}")
        self.last = body


class Fan(HttpUser):
    """Someone refreshing the availability page."""

    weight = 3
    wait_time = between(0.2, 1)
    etag: str | None = None

    @task
    def refresh(self) -> None:
        headers = {"If-None-Match": self.etag} if self.etag else {}
        with self.client.get(
            "/api/v1/events/LOADTEST/availability", headers=headers, catch_response=True, name="availability"
        ) as reply:
            if reply.status_code in (200, 304):
                self.etag = reply.headers.get("ETag", self.etag)
                reply.success()
            else:
                reply.failure(str(reply.status_code))
