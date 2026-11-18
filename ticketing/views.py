import dataclasses
import datetime as dt
import hashlib
import json
import time

import httpx
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Count
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from mock_api.app import Faults

from .models import ConnectorMetric, ConnectorState, DeadLetter, OutboundCheckin, SyncCursor
from .resilience import CircuitBreaker, RetryPolicy
from .security import OperatorToken, PersonalDataReader, webhook_is_authentic
from .services import apply_offline_scan, event_availability, event_manifest, handle_webhook, scan_ticket

AVAILABILITY_TTL_SECONDS = 2
MANIFEST_PAGE = 5000
OFFLINE_BATCH_LIMIT = 1000
# Device clocks drift. A scan this far ahead of the server is not trusted.
OFFLINE_CLOCK_SKEW = dt.timedelta(minutes=5)


@csrf_exempt
@require_POST
def ticketing_webhook(request: HttpRequest) -> JsonResponse:
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")
    if not webhook_is_authentic(request.body, timestamp, signature):
        return JsonResponse({"error": "bad signature"}, status=401)
    try:
        payload = json.loads(request.body)
        event_id = payload["id"]
        if not isinstance(event_id, str) or not event_id:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": "malformed event"}, status=400)
    return JsonResponse({"result": handle_webhook(event_id, payload)})


class ScanView(APIView):
    permission_classes = [OperatorToken]

    def post(self, request: Request) -> Response:
        ticket, scan_id = request.data.get("ticket"), request.data.get("scan_id")
        if not (
            isinstance(ticket, str) and ticket and isinstance(scan_id, str) and scan_id and len(scan_id) <= 64
        ):
            return Response({"error": "ticket and scan_id are required"}, status=400)
        result = scan_ticket(ticket, scan_id)
        return Response(
            {"outcome": result.outcome, "checked_in_at": result.checked_in_at}, status=result.http_status
        )


class EventManifestView(APIView):
    permission_classes = [PersonalDataReader]

    def get(self, request: Request, event: str) -> Response:
        try:
            after = max(0, int(request.query_params.get("after", 0)))
        except ValueError:
            return Response({"error": "after must be a number"}, status=400)
        return Response(event_manifest(event, after, MANIFEST_PAGE))


def _offline_scan_fields(item: object, latest: dt.datetime) -> tuple[str, str, dt.datetime] | None:
    if not isinstance(item, dict):
        return None
    ticket, scan_id, raw_time = item.get("ticket"), item.get("scan_id"), item.get("scanned_at")
    if not (isinstance(ticket, str) and 0 < len(ticket) <= 64 and isinstance(scan_id, str) and 0 < len(scan_id) <= 64):
        return None
    try:
        when = parse_datetime(raw_time) if isinstance(raw_time, str) else None
    except ValueError:
        return None
    if when is None or when.tzinfo is None or when > latest:
        return None
    return ticket, scan_id, when


class OfflineScanUploadView(APIView):
    """A gate that was offline uploads what it decided. Safe to send again after a lost reply."""

    permission_classes = [OperatorToken]

    def post(self, request: Request) -> Response:
        device, scans = request.data.get("device"), request.data.get("scans")
        if not (isinstance(device, str) and 0 < len(device) <= 64 and isinstance(scans, list)):
            return Response({"error": "device and scans are required"}, status=400)
        if len(scans) > OFFLINE_BATCH_LIMIT:
            return Response({"error": f"At most {OFFLINE_BATCH_LIMIT} scans per upload"}, status=413)
        latest = timezone.now() + OFFLINE_CLOCK_SKEW
        results = []
        for item in scans:
            fields = _offline_scan_fields(item, latest)
            if fields is None:
                scan_id = item.get("scan_id") if isinstance(item, dict) else None
                results.append({"scan_id": scan_id if isinstance(scan_id, str) else None, "result": "rejected"})
                continue
            ticket, scan_id, when = fields
            results.append({"scan_id": scan_id, "result": apply_offline_scan(ticket, scan_id, device, when)})
        return Response({"results": results})


def cached_availability(event: str) -> tuple[dict, bool]:
    """One worker recomputes while the others serve the last copy, so a refresh storm costs one query."""
    digest = hashlib.sha256(event.encode()).hexdigest()
    key, stale_key, lock = f"avail:{digest}", f"avail-stale:{digest}", f"avail-lock:{digest}"
    data = cache.get(key)
    if data is not None:
        return data, True
    if cache.add(lock, 1, timeout=5):
        data = event_availability(event)
        cache.set(key, data, AVAILABILITY_TTL_SECONDS)
        cache.set(stale_key, data, 300)
        cache.delete(lock)
        return data, False
    data = cache.get(stale_key)
    if data is not None:
        return data, True
    return event_availability(event), False


class AvailabilityView(APIView):
    """The read everyone refreshes at once."""

    def get(self, request: Request, event: str) -> Response:
        data, cached = cached_availability(event)
        etag = '"' + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16] + '"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status=304, headers={"ETag": etag})
        return Response(data, headers={"ETag": etag, "X-Cache": "hit" if cached else "miss"})


def event_page(request: HttpRequest, event: str) -> HttpResponse:
    """A public page for one event. It sets no cookie, so the edge can cache it for everyone."""
    data, _ = cached_availability(event)
    return render(request, "console/event.html", {"event": event, "availability": data})


def seconds_until_probe(state: ConnectorState | None) -> float | None:
    if state is None or state.state != "open" or state.opened_at is None:
        return None
    return max(0.0, state.opened_at + CircuitBreaker().reset_timeout - time.time())


def cursor_lag(cursor: SyncCursor | None) -> int | None:
    """Items behind the feed head. Unknown stays None. The cursor is numeric only for APIs that say so."""
    if cursor is None or cursor.feed_head is None:
        return None
    try:
        return max(0, cursor.feed_head - int(cursor.cursor or 0))
    except ValueError:
        return None


def dead_letter_summary() -> list[dict]:
    rows = (
        DeadLetter.objects.filter(resolved=False)
        .values("kind")
        .annotate(count=Count("pk"))
        .order_by("-count")
    )
    return [{"kind": row["kind"], "count": row["count"]} for row in rows]


class DeadLetterListView(APIView):
    permission_classes = [PersonalDataReader]

    def get(self, request: Request) -> Response:
        rows = DeadLetter.objects.filter(resolved=False)[:100]
        return Response(
            [
                {
                    "id": row.pk,
                    "kind": row.kind,
                    "reason": row.reason,
                    "payload": row.payload,
                    "created_at": row.created_at,
                }
                for row in rows
            ]
        )


class DeadLetterResolveView(APIView):
    """A person looked at this message and closed it. The record stays, marked resolved."""

    permission_classes = [OperatorToken]

    def post(self, request: Request, pk: int) -> Response:
        updated = DeadLetter.objects.filter(pk=pk, resolved=False).update(resolved=True)
        return Response({"resolved": bool(updated)}, status=200 if updated else 404)


class DemoFaultsView(APIView):
    """Console control for the stand-in API. It can only ever reach the configured mock."""

    permission_classes = [OperatorToken]

    def get(self, request: Request) -> Response:
        return self._forward("GET", None)

    def post(self, request: Request) -> Response:
        allowed = {f.name for f in dataclasses.fields(Faults)}
        body = {key: value for key, value in request.data.items() if key in allowed}
        return self._forward("POST", body)

    def _forward(self, method: str, body: dict | None) -> Response:
        if not settings.TICKETING_IS_MOCK:
            return Response({"error": "Fault injection is available only with the stand-in API"}, status=404)
        try:
            reply = httpx.request(method, f"{settings.TICKETING_BASE_URL}/_faults", json=body, timeout=5)
            stats = httpx.get(f"{settings.TICKETING_BASE_URL}/_stats", timeout=5).json()
        except httpx.HTTPError as exc:
            return Response({"error": f"The stand-in API did not answer: {type(exc).__name__}"}, status=502)
        return Response({"faults": reply.json(), "stats": stats}, status=reply.status_code)


class ConnectorHealthView(APIView):
    def get(self, request: Request) -> Response:
        state = ConnectorState.objects.filter(name="ticketing").first()
        cursor = SyncCursor.objects.filter(name="tickets").first()
        metrics = list(ConnectorMetric.objects.all()[:30])
        return Response(
            {
                "breaker": state.state if state else "closed",
                "times_opened": state.times_opened if state else 0,
                "consecutive_failures": state.consecutive_failures if state else 0,
                "failure_threshold": CircuitBreaker().failure_threshold,
                "seconds_until_probe": seconds_until_probe(state),
                "feed_head": cursor.feed_head if cursor else None,
                "cursor_lag": cursor_lag(cursor),
                "last_success_at": cursor.last_success_at if cursor else None,
                "dead_letters": dead_letter_summary(),
                "retry_policy": {
                    "max_attempts": RetryPolicy().max_attempts,
                    "base_delay": RetryPolicy().base_delay,
                    "max_delay": RetryPolicy().max_delay,
                    "deadline": RetryPolicy().deadline,
                },
                "rate_limit_per_second": settings.TICKETING_RATE_PER_SECOND,
                "cursor": cursor.cursor if cursor else "",
                "pages_synced": cursor.pages_synced if cursor else 0,
                "items_applied": cursor.items_applied if cursor else 0,
                "items_stale": cursor.items_stale if cursor else 0,
                "caught_up": cursor.caught_up if cursor else False,
                "outbox_pending": OutboundCheckin.objects.filter(status="pending").count(),
                "outbox_dead": OutboundCheckin.objects.filter(status="dead").count(),
                "dead_letters_open": DeadLetter.objects.filter(resolved=False).count(),
                "metrics": [
                    {
                        "minute": m.minute,
                        "requests": m.requests,
                        "successes": m.successes,
                        "retries": m.retries,
                        "rate_limited": m.rate_limited,
                        "server_errors": m.server_errors,
                        "timeouts": m.timeouts,
                        "client_errors": m.client_errors,
                        "breaker_rejections": m.breaker_rejections,
                        "latency_buckets": m.latency_buckets,
                    }
                    for m in metrics
                ],
                "database": connection.vendor,
            }
        )
