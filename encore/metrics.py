"""Prometheus text format, read from the database.

Gauges come from shared state, so it does not matter which Gunicorn worker answers a scrape. The edge refuses this
path, so only something inside the network can read it. Alerts on these names are in deploy/k8s/alerts.yaml.
"""

from django.db import DatabaseError, connection
from django.db.models import Min
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone

from imports.models import ImportRun, QuarantineRow
from ticketing.models import ConnectorState, DeadLetter, OutboundCheckin, ScanConflict, SyncCursor


def _line(name: str, value: float, help_text: str) -> str:
    return f"# HELP {name} {help_text}\n# TYPE {name} gauge\n{name} {value}\n"


def _age(when) -> float:
    return (timezone.now() - when).total_seconds() if when else 0.0


def render_metrics() -> str:
    running = ImportRun.objects.filter(status=ImportRun.Status.RUNNING)
    oldest_beat = running.aggregate(value=Min("heartbeat_at"))["value"]
    pending = OutboundCheckin.objects.filter(status="pending")
    oldest_pending = pending.aggregate(value=Min("created_at"))["value"]
    state = ConnectorState.objects.filter(name="ticketing").first()
    cursor = SyncCursor.objects.filter(name="tickets").first()
    lag = 0.0
    if cursor and cursor.feed_head is not None:
        try:
            lag = max(0.0, float(cursor.feed_head) - float(cursor.cursor or 0))
        except ValueError:
            lag = 0.0
    rows = [
        ("encore_imports_running", running.count(), "Imports running now"),
        ("encore_import_heartbeat_age_seconds", _age(oldest_beat), "Oldest heartbeat among running imports"),
        (
            "encore_imports_failed",
            ImportRun.objects.filter(status=ImportRun.Status.FAILED).count(),
            "Imports that stopped with an error",
        ),
        (
            "encore_imports_unbalanced",
            ImportRun.objects.filter(reconciliation__balanced=False).count(),
            "Finished imports whose reconciliation found a discrepancy",
        ),
        (
            "encore_quarantine_open",
            QuarantineRow.objects.filter(status=QuarantineRow.Status.OPEN).count(),
            "Rows waiting for a person",
        ),
        ("encore_outbox_pending", pending.count(), "Check-ins not delivered yet"),
        ("encore_outbox_oldest_pending_seconds", _age(oldest_pending), "Age of the oldest undelivered check-in"),
        ("encore_outbox_dead", OutboundCheckin.objects.filter(status="dead").count(), "Check-ins that gave up"),
        ("encore_dead_letters_open", DeadLetter.objects.filter(resolved=False).count(), "Messages needing a person"),
        ("encore_scan_conflicts_open", ScanConflict.objects.filter(reviewed=False).count(), "Offline scans to review"),
        ("encore_breaker_open", 1 if state and state.state == "open" else 0, "1 while the circuit breaker is open"),
        ("encore_sync_cursor_lag_items", lag, "Items behind the ticketing feed"),
        (
            "encore_sync_seconds_since_success",
            _age(cursor.last_success_at) if cursor else 0.0,
            "Seconds since a ticket page was last applied",
        ),
    ]
    return "".join(_line(*row) for row in rows)


def metrics(_request: HttpRequest) -> HttpResponse:
    return HttpResponse(render_metrics(), content_type="text/plain; version=0.0.4; charset=utf-8")


def healthz(_request: HttpRequest) -> JsonResponse:
    """Ready for traffic: the database answers."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"ok": False}, status=503)
    return JsonResponse({"ok": True})


def livez(_request: HttpRequest) -> JsonResponse:
    """The process answers. It does not touch the database, so a database outage does not restart every pod."""
    return JsonResponse({"ok": True})
