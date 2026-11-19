# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""A stand-in for an external ticketing API, with failures you can switch on during a demo.

It holds a feed of ticket versions: every ticket once, then later updates to some of them. Check-ins record
how many real side effects happened, so a test can prove that retries did not double-apply anything.
"""

import asyncio
import os
import random
from dataclasses import asdict, dataclass

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


@dataclass
class Faults:
    p_429: float = 0.0
    p_500: float = 0.0
    p_timeout: float = 0.0
    timeout_seconds: float = 10.0
    p_duplicate: float = 0.0
    p_shuffle: float = 0.0
    p_malformed: float = 0.0
    retry_after: float = 0.05
    outage: bool = False


class State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.tickets = int(os.environ.get("MOCK_TICKETS", "5000"))
        self.updates = max(1, self.tickets // 20)
        self.faults = Faults()
        self.rng = random.Random(11)
        self.requests = 0
        self.injected = {"429": 0, "500": 0, "timeout": 0, "duplicate": 0, "shuffle": 0, "malformed": 0}
        self.checkins: dict[str, dict] = {}
        self.side_effects = 0

    def item(self, position: int) -> dict:
        if position < self.tickets:
            number, version, status = position, 1, "valid"
        else:
            number = (position - self.tickets) * 20 % self.tickets
            version, status = 2, "cancelled"
        return {
            "id": f"T{number:08d}",
            "event": f"E{number % 5:03d}",
            "email": f"holder{number}@example.test",
            "status": status,
            "version": version,
        }


state = State()


async def _maybe_fail() -> JSONResponse | None:
    faults = state.faults
    state.requests += 1
    if faults.outage:
        state.injected["500"] += 1
        return JSONResponse({"error": "outage"}, status_code=503)
    roll = state.rng.random()
    if roll < faults.p_429:
        state.injected["429"] += 1
        return JSONResponse(
            {"error": "slow down"}, status_code=429, headers={"Retry-After": str(faults.retry_after)}
        )
    roll -= faults.p_429
    if roll < faults.p_500:
        state.injected["500"] += 1
        return JSONResponse({"error": "boom"}, status_code=500)
    roll -= faults.p_500
    if roll < faults.p_timeout:
        state.injected["timeout"] += 1
        await asyncio.sleep(faults.timeout_seconds)
    return None


async def tickets(request: Request) -> JSONResponse:
    failure = await _maybe_fail()
    if failure:
        return failure
    start = int(request.query_params.get("cursor") or 0)
    limit = min(int(request.query_params.get("limit", 500)), 1000)
    total = state.tickets + state.updates
    end = min(start + limit, total)
    items: list = [state.item(position) for position in range(start, end)]
    faults = state.faults
    if items and state.rng.random() < faults.p_duplicate:
        items.append(dict(state.rng.choice(items)))
        state.injected["duplicate"] += 1
    if items and state.rng.random() < faults.p_shuffle:
        items.reverse()
        state.injected["shuffle"] += 1
    if items and state.rng.random() < faults.p_malformed:
        items.append({"id": None, "status": "???", "page": start})
        state.injected["malformed"] += 1
    return JSONResponse(
        {"items": items, "next_cursor": str(end), "has_more": end < total, "feed_head": total}
    )


async def checkins(request: Request) -> JSONResponse:
    key = request.headers.get("Idempotency-Key")
    if not key:
        return JSONResponse({"error": "Idempotency-Key is required"}, status_code=400)
    failure = await _maybe_fail()
    if failure:
        return failure
    body = await request.json()
    if not body.get("ticket"):
        return JSONResponse({"error": "ticket is required"}, status_code=422)
    if key not in state.checkins:
        state.side_effects += 1
        state.checkins[key] = {"ticket": body["ticket"], "accepted": True, "sequence": state.side_effects}
    return JSONResponse(state.checkins[key])


async def faults_endpoint(request: Request) -> JSONResponse:
    if request.method == "POST":
        try:
            state.faults = Faults(**await request.json())
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(asdict(state.faults))


async def stats(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "requests": state.requests,
            "injected": state.injected,
            "side_effects": state.side_effects,
            "feed_size": state.tickets + state.updates,
            "tickets": state.tickets,
        }
    )


async def reset(_request: Request) -> JSONResponse:
    state.reset()
    return JSONResponse({"ok": True})


app = Starlette(
    routes=[
        Route("/v1/tickets", tickets),
        Route("/v1/checkins", checkins, methods=["POST"]),
        Route("/_faults", faults_endpoint, methods=["GET", "POST"]),
        Route("/_stats", stats),
        Route("/_reset", reset, methods=["POST"]),
    ]
)
