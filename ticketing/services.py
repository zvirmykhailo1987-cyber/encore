"""Sync, webhook handling, check-in and delivery. Each one commits its data and progress together."""

import datetime as dt
import hashlib
import json
from dataclasses import dataclass

import httpx
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from .client import CallStats, CircuitOpen, ConnectorError, PermanentError, TicketingClient
from .models import (
    ConnectorMetric,
    ConnectorState,
    DeadLetter,
    OutboundCheckin,
    SyncCursor,
    Ticket,
    WebhookEvent,
)
from .resilience import CircuitBreaker, RetryPolicy, TokenBucket

CURSOR_NAME = "tickets"
OUTBOX_MAX_ATTEMPTS = 8
STATUSES = {"valid", "cancelled", "refunded"}


def dead_letter(kind: str, payload: dict, reason: str) -> None:
    key = hashlib.sha256(json.dumps([kind, payload], sort_keys=True, default=str).encode()).hexdigest()
    DeadLetter.objects.get_or_create(
        dedupe_key=key, defaults={"kind": kind, "payload": payload, "reason": reason[:300]}
    )


def parse_ticket(item: object) -> tuple[str, str, str | None, str, int] | None:
    if not isinstance(item, dict):
        return None
    ticket_id, event, version, status = (
        item.get("id"),
        item.get("event"),
        item.get("version"),
        item.get("status"),
    )
    if not (isinstance(ticket_id, str) and ticket_id and isinstance(event, str) and event):
        return None
    if not isinstance(version, int) or isinstance(version, bool) or version < 1 or status not in STATUSES:
        return None
    email = item.get("email")
    return ticket_id, event, email if isinstance(email, str) and email else None, status, version


def apply_ticket_versions(rows: list[tuple[str, str, str | None, str, int]]) -> int:
    """Insert or update, only ever moving a ticket forward. Check-in state is ours and is never touched.

    The version test is inside one SQL statement, so a late webhook and a page sync cannot overwrite a
    newer version whichever commits first.
    """
    newest: dict[str, tuple[str, str, str | None, str, int]] = {}
    for row in rows:
        if row[0] not in newest or row[4] > newest[row[0]][4]:
            newest[row[0]] = row
    if not newest:
        return 0
    ordered = [newest[key] for key in sorted(newest)]
    columns = list(zip(*ordered, strict=True))
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ticketing_ticket (external_id, event_id_external, holder_email, status, version)
            SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::int[])
            ON CONFLICT (external_id) DO UPDATE SET
                event_id_external = EXCLUDED.event_id_external,
                holder_email = EXCLUDED.holder_email,
                status = EXCLUDED.status,
                version = EXCLUDED.version
            WHERE ticketing_ticket.version < EXCLUDED.version
            """,
            [list(column) for column in columns],
        )
        return cursor.rowcount


@dataclass
class SyncResult:
    pages: int = 0
    applied: int = 0
    stale: int = 0
    dead_lettered: int = 0
    caught_up: bool = False
    stopped_by: str = ""


def sync_tickets(client: TicketingClient, max_pages: int = 200, limit: int = 500) -> SyncResult:
    """Pull pages from the saved cursor. A page and its cursor commit together, so a crash repeats at most one page."""
    result = SyncResult()
    SyncCursor.objects.get_or_create(name=CURSOR_NAME)
    for _ in range(max_pages):
        cursor_value = SyncCursor.objects.get(name=CURSOR_NAME).cursor
        try:
            page = client.fetch_tickets(cursor_value, limit)
        except ConnectorError as exc:
            result.stopped_by = str(exc)
            break
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            dead_letter("ticket_page", {"cursor": cursor_value}, "The page has no item list")
            result.stopped_by = "malformed page"
            break
        parsed, bad = [], []
        for item in items:
            row = parse_ticket(item)
            if row is None:
                bad.append(item)
            else:
                parsed.append(row)
        next_cursor = str(page.get("next_cursor") or cursor_value)
        head = page.get("feed_head")
        with transaction.atomic():
            # Serialises page commits. A second worker that fetched the same page sees the cursor moved and backs off.
            current = SyncCursor.objects.select_for_update().get(name=CURSOR_NAME)
            if current.cursor != cursor_value:
                result.stopped_by = "another worker advanced the cursor"
                break
            applied = apply_ticket_versions(parsed)
            for item in bad:
                dead_letter("ticket_item", {"item": item, "cursor": cursor_value}, "Failed validation")
            SyncCursor.objects.filter(pk=current.pk).update(
                cursor=next_cursor,
                pages_synced=F("pages_synced") + 1,
                items_applied=F("items_applied") + applied,
                items_stale=F("items_stale") + len(parsed) - applied,
                last_success_at=timezone.now(),
                caught_up=not page.get("has_more"),
                feed_head=head if isinstance(head, int) else current.feed_head,
            )
        result.pages += 1
        result.applied += applied
        result.stale += len(parsed) - applied
        result.dead_lettered += len(bad)
        if not page.get("has_more"):
            result.caught_up = True
            break
    return result


def handle_webhook(event_id: str, payload: dict) -> str:
    """Providers deliver at least once, often out of order. Returns duplicate, applied, stale or dead_letter."""
    with transaction.atomic():
        try:
            with transaction.atomic():
                WebhookEvent.objects.create(event_id=event_id, payload=payload)
        except IntegrityError:
            return "duplicate"
        row = parse_ticket(payload.get("ticket"))
        if row is None:
            dead_letter("webhook", {"event_id": event_id, "payload": payload}, "Failed validation")
            return "dead_letter"
        return "applied" if apply_ticket_versions([row]) else "stale"


@dataclass
class ScanResult:
    outcome: str
    http_status: int
    checked_in_at: dt.datetime | None = None


def scan_ticket(ticket_external_id: str, scan_id: str) -> ScanResult:
    """Admit a ticket once. A gate retrying the same scan after a lost reply gets the same answer."""
    now = timezone.now()
    with transaction.atomic():
        admitted = Ticket.objects.filter(
            external_id=ticket_external_id, checked_in_at__isnull=True, status="valid"
        ).update(checked_in_at=now, checked_in_scan_id=scan_id)
        if admitted:
            OutboundCheckin.objects.create(ticket_external_id=ticket_external_id, idempotency_key=scan_id)
            return ScanResult("admitted", 200, now)
    ticket = Ticket.objects.filter(external_id=ticket_external_id).first()
    if ticket is None:
        return ScanResult("unknown_ticket", 404)
    if ticket.checked_in_scan_id == scan_id:
        return ScanResult("admitted", 200, ticket.checked_in_at)
    if ticket.status != "valid":
        return ScanResult(f"ticket_{ticket.status}", 409)
    return ScanResult("already_used", 409, ticket.checked_in_at)


def event_availability(event_external_id: str) -> dict:
    return Ticket.objects.filter(event_id_external=event_external_id).aggregate(
        issued=Count("pk"),
        valid=Count("pk", filter=Q(status="valid")),
        checked_in=Count("pk", filter=Q(checked_in_at__isnull=False)),
    )


@dataclass
class FlushResult:
    sent: int = 0
    retried: int = 0
    dead: int = 0
    stopped_by: str = ""


def flush_outbox(client: TicketingClient, limit: int = 200) -> FlushResult:
    """Deliver due check-ins. This client makes one attempt per message: the outbox itself owns the retry schedule."""
    result = FlushResult()
    for _ in range(limit):
        with transaction.atomic():
            message = (
                OutboundCheckin.objects.select_for_update(skip_locked=True)
                .filter(status=OutboundCheckin.Status.PENDING, next_attempt_at__lte=timezone.now())
                .order_by("next_attempt_at", "pk")
                .first()
            )
            if message is None:
                break
            message.attempts += 1
            try:
                client.post_checkin(message.ticket_external_id, message.idempotency_key)
            except CircuitOpen as exc:
                # Nothing reached the API, so this attempt does not count against the message.
                result.stopped_by = str(exc)
                break
            except PermanentError as exc:
                message.status = OutboundCheckin.Status.DEAD
                message.last_error = str(exc)[:300]
                dead_letter("checkin", {"key": message.idempotency_key}, str(exc))
                result.dead += 1
            except ConnectorError as exc:
                message.last_error = str(exc)[:300]
                if message.attempts >= OUTBOX_MAX_ATTEMPTS:
                    message.status = OutboundCheckin.Status.DEAD
                    dead_letter("checkin", {"key": message.idempotency_key}, str(exc))
                    result.dead += 1
                else:
                    message.next_attempt_at = timezone.now() + dt.timedelta(
                        seconds=min(300, 2**message.attempts)
                    )
                    result.retried += 1
            else:
                message.status = OutboundCheckin.Status.SENT
                message.last_error = ""
                result.sent += 1
            message.save()
    return result


def save_stats(stats: CallStats) -> None:
    if not stats.counts and not stats.latency:
        return
    minute = timezone.now().replace(second=0, microsecond=0)
    with transaction.atomic():
        metric, _ = ConnectorMetric.objects.select_for_update().get_or_create(minute=minute)
        names = (
            "requests",
            "successes",
            "retries",
            "rate_limited",
            "server_errors",
            "timeouts",
            "client_errors",
        )
        for name in names:
            setattr(metric, name, getattr(metric, name) + stats.counts[name])
        metric.breaker_rejections += stats.counts["breaker_rejections"]
        buckets = dict(metric.latency_buckets)
        for bound, count in stats.latency.items():
            buckets[bound] = buckets.get(bound, 0) + count
        metric.latency_buckets = buckets
        metric.save()


def _load_breaker() -> CircuitBreaker:
    state, _ = ConnectorState.objects.get_or_create(name="ticketing")
    breaker = CircuitBreaker()
    breaker.state = state.state
    breaker.consecutive_failures = state.consecutive_failures
    breaker.opened_at = state.opened_at
    breaker.times_opened = state.times_opened
    return breaker


def _save_breaker(breaker: CircuitBreaker) -> None:
    ConnectorState.objects.filter(name="ticketing").update(
        state=breaker.state,
        consecutive_failures=breaker.consecutive_failures,
        opened_at=breaker.opened_at,
        times_opened=breaker.times_opened,
    )


def run_with_client(work, policy: RetryPolicy | None = None, timeout: httpx.Timeout | None = None):
    """Build a client from shared state, run the work, then publish breaker state and metrics."""
    breaker = _load_breaker()
    client = TicketingClient(
        settings.TICKETING_BASE_URL,
        breaker=breaker,
        bucket=TokenBucket(settings.TICKETING_RATE_PER_SECOND),
        policy=policy,
        timeout=timeout,
    )
    try:
        return work(client)
    finally:
        client.close()
        _save_breaker(breaker)
        save_stats(client.stats)
