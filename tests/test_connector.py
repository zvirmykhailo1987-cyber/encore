# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

import json
import threading
import time
import uuid

import httpx
import pytest
from django.db import connection
from rest_framework.test import APIClient

from ticketing.client import CircuitOpen, PermanentError, RetriesExhausted, TicketingClient
from ticketing.models import ConnectorMetric, DeadLetter, OutboundCheckin, SyncCursor, Ticket
from ticketing.resilience import CircuitBreaker, RetryPolicy
from ticketing.security import sign_webhook, webhook_is_authentic
from ticketing.services import (
    apply_ticket_versions,
    flush_outbox,
    handle_webhook,
    save_stats,
    scan_ticket,
    sync_tickets,
)

FAST = RetryPolicy(max_attempts=6, base_delay=0.001, max_delay=0.01, deadline=20)
QUICK_TIMEOUT = httpx.Timeout(0.3, connect=1.0)


def faults(url: str, **values) -> None:
    httpx.post(f"{url}/_faults", json=values).raise_for_status()


def stats(url: str) -> dict:
    return httpx.get(f"{url}/_stats").json()


def client_for(url: str, **kwargs) -> TicketingClient:
    kwargs.setdefault("policy", FAST)
    kwargs.setdefault("timeout", QUICK_TIMEOUT)
    kwargs.setdefault("breaker", CircuitBreaker(failure_threshold=50))
    return TicketingClient(url, **kwargs)


def expected_total(url: str) -> int:
    return stats(url)["tickets"]


def test_retries_through_429_and_500_and_counts_them(mock_api):
    faults(mock_api, p_429=0.3, p_500=0.3, retry_after=0.001)
    client = client_for(mock_api, policy=RetryPolicy(max_attempts=15, base_delay=0.001, max_delay=0.01))
    for _ in range(10):
        assert "items" in client.fetch_tickets("", 10)
    assert client.stats.counts["successes"] == 10
    assert client.stats.counts["retries"] > 0
    assert client.stats.counts["rate_limited"] > 0
    assert client.stats.counts["server_errors"] > 0


def test_retry_after_header_is_honoured(mock_api):
    faults(mock_api, p_429=1.0, retry_after=0.05)
    slept: list[float] = []
    client = client_for(mock_api, sleep=slept.append, policy=RetryPolicy(max_attempts=3, base_delay=0.001))
    with pytest.raises(RetriesExhausted):
        client.fetch_tickets("")
    assert len(slept) == 2
    assert all(value >= 0.05 for value in slept)


def test_slow_response_times_out_and_is_retried(mock_api):
    faults(mock_api, p_timeout=1.0, timeout_seconds=2)
    client = client_for(mock_api, policy=RetryPolicy(max_attempts=2, base_delay=0.001))
    started = time.monotonic()
    with pytest.raises(RetriesExhausted):
        client.fetch_tickets("")
    assert time.monotonic() - started < 1.5
    assert client.stats.counts["timeouts"] == 2


def test_rate_limiting_does_not_trip_the_breaker(mock_api):
    faults(mock_api, p_429=1.0)
    breaker = CircuitBreaker(failure_threshold=2)
    client = client_for(mock_api, breaker=breaker, policy=RetryPolicy(max_attempts=5, base_delay=0.001))
    with pytest.raises(RetriesExhausted):
        client.fetch_tickets("")
    assert breaker.state == "closed"


def test_outage_opens_breaker_and_stops_traffic_then_recovers(mock_api):
    faults(mock_api, outage=True)
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout=1.5)
    client = client_for(mock_api, breaker=breaker, policy=RetryPolicy(max_attempts=10, base_delay=0.001))
    with pytest.raises(CircuitOpen):
        client.fetch_tickets("")
    sent = stats(mock_api)["requests"]
    assert sent == 3
    with pytest.raises(CircuitOpen):
        client.fetch_tickets("")
    assert stats(mock_api)["requests"] == sent, "An open breaker must not send anything"

    faults(mock_api)
    time.sleep(1.6)
    assert "items" in client.fetch_tickets("", 5)
    assert breaker.state == "closed"


def test_client_errors_are_not_retried(mock_api):
    client = client_for(mock_api)
    with pytest.raises(PermanentError):
        client.post_checkin("", "key-1")
    assert client.stats.counts["requests"] == 1


def test_post_refuses_to_run_without_an_idempotency_key(mock_api):
    with pytest.raises(ValueError, match="idempotency"):
        client_for(mock_api).post_checkin("T1", "")


def test_lost_reply_retried_with_same_key_applies_once(mock_api):
    client = client_for(mock_api)
    first = client.post_checkin("T00000001", "same-key")
    second = client.post_checkin("T00000001", "same-key")
    assert first == second
    assert stats(mock_api)["side_effects"] == 1


@pytest.mark.django_db(transaction=True)
def test_full_sync_under_chaos_matches_the_source_exactly(mock_api):
    faults(
        mock_api, p_429=0.15, p_500=0.15, p_duplicate=0.3, p_shuffle=0.3, p_malformed=0.2, retry_after=0.001
    )
    result = sync_tickets(client_for(mock_api), limit=100)
    assert result.caught_up is True
    injected = stats(mock_api)["injected"]
    assert min(injected["429"], injected["500"], injected["duplicate"], injected["malformed"]) > 0
    assert Ticket.objects.count() == expected_total(mock_api)
    # Every twentieth ticket has a v2 update in the feed. Each must end on its newest version.
    updates = stats(mock_api)["feed_size"] - expected_total(mock_api)
    assert Ticket.objects.filter(version=2, status="cancelled").count() == updates
    assert DeadLetter.objects.filter(kind="ticket_item").count() == injected["malformed"]


@pytest.mark.django_db(transaction=True)
def test_sync_stops_at_outage_and_continues_from_the_cursor(mock_api):
    assert sync_tickets(client_for(mock_api), max_pages=3, limit=100).pages == 3
    assert SyncCursor.objects.get().cursor == "300"
    faults(mock_api, outage=True)
    stopped = sync_tickets(
        client_for(mock_api, policy=RetryPolicy(max_attempts=2, base_delay=0.001)), limit=100
    )
    assert stopped.pages == 0
    assert stopped.stopped_by
    assert SyncCursor.objects.get().cursor == "300"
    faults(mock_api)
    resumed = sync_tickets(client_for(mock_api), limit=100)
    assert resumed.caught_up
    assert Ticket.objects.count() == expected_total(mock_api)


@pytest.mark.django_db(transaction=True)
def test_a_worker_with_a_stale_page_does_not_move_the_cursor_back(mock_api):
    sync_tickets(client_for(mock_api), max_pages=2, limit=100)
    SyncCursor.objects.update(cursor="900")

    class Racing(TicketingClient):
        def fetch_tickets(self, cursor, limit=500):
            page = super().fetch_tickets(cursor, limit)
            SyncCursor.objects.update(cursor="950")
            return page

    result = sync_tickets(Racing(mock_api, policy=FAST), max_pages=1, limit=10)
    assert result.pages == 0
    assert SyncCursor.objects.get().cursor == "950"


@pytest.mark.django_db
def test_older_version_never_overwrites_newer():
    apply_ticket_versions([("T1", "E1", "a@x.io", "cancelled", 3)])
    assert apply_ticket_versions([("T1", "E1", "old@x.io", "valid", 2)]) == 0
    ticket = Ticket.objects.get()
    assert (ticket.status, ticket.version, ticket.holder_email) == ("cancelled", 3, "a@x.io")


@pytest.mark.django_db
def test_upstream_update_keeps_local_checkin():
    apply_ticket_versions([("T1", "E1", None, "valid", 1)])
    assert scan_ticket("T1", "scan-1").outcome == "admitted"
    apply_ticket_versions([("T1", "E1", "new@x.io", "valid", 2)])
    ticket = Ticket.objects.get()
    assert ticket.checked_in_at is not None
    assert ticket.holder_email == "new@x.io"


@pytest.mark.django_db
def test_webhook_duplicate_and_out_of_order():
    ticket = {"id": "T9", "event": "E1", "status": "valid", "version": 2}
    assert handle_webhook("ev-2", {"ticket": ticket}) == "applied"
    assert handle_webhook("ev-2", {"ticket": ticket}) == "duplicate"
    assert handle_webhook("ev-1", {"ticket": {**ticket, "version": 1, "status": "cancelled"}}) == "stale"
    assert Ticket.objects.get().status == "valid"
    assert handle_webhook("ev-3", {"ticket": {"id": 5}}) == "dead_letter"
    assert DeadLetter.objects.filter(kind="webhook").count() == 1


def test_webhook_signature_rules(settings):
    settings.TICKETING_WEBHOOK_SECRET = "s3"
    body = b'{"id":"a"}'
    now = 1_700_000_000.0
    good = sign_webhook("s3", str(now), body)
    assert webhook_is_authentic(body, str(now), good, now=now)
    assert not webhook_is_authentic(body + b" ", str(now), good, now=now)
    assert not webhook_is_authentic(body, str(now), good, now=now + 301)
    assert not webhook_is_authentic(body, "nonsense", good, now=now)
    settings.TICKETING_WEBHOOK_SECRET = ""
    assert not webhook_is_authentic(body, str(now), good, now=now)


@pytest.mark.django_db
def test_webhook_endpoint_rejects_bad_signature_and_accepts_good(settings):
    settings.TICKETING_WEBHOOK_SECRET = "s3"
    api = APIClient()
    body = json.dumps({"id": "ev-1", "ticket": {"id": "T1", "event": "E", "status": "valid", "version": 1}})
    assert api.post("/api/v1/webhooks/ticketing", body, content_type="application/json").status_code == 401
    stamp = str(time.time())
    response = api.post(
        "/api/v1/webhooks/ticketing",
        body,
        content_type="application/json",
        HTTP_X_TIMESTAMP=stamp,
        HTTP_X_SIGNATURE=sign_webhook("s3", stamp, body.encode()),
    )
    assert response.status_code == 200
    assert response.json()["result"] == "applied"


@pytest.mark.django_db
def test_scan_outcomes():
    apply_ticket_versions([("T1", "E", None, "valid", 1), ("T2", "E", None, "cancelled", 1)])
    assert scan_ticket("T1", "s1").outcome == "admitted"
    assert scan_ticket("T1", "s1").outcome == "admitted", "A retry of the same scan gets the same answer"
    second = scan_ticket("T1", "s2")
    assert (second.outcome, second.http_status) == ("already_used", 409)
    assert scan_ticket("T2", "s3").outcome == "ticket_cancelled"
    assert scan_ticket("nope", "s4").http_status == 404
    assert OutboundCheckin.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_same_ticket_scanned_at_many_gates_admits_exactly_one():
    apply_ticket_versions([("T1", "E", None, "valid", 1)])
    outcomes: list[str] = []
    barrier = threading.Barrier(12)

    def gate(number: int) -> None:
        barrier.wait()
        outcomes.append(scan_ticket("T1", f"scan-{number}").outcome)
        connection.close()

    threads = [threading.Thread(target=gate, args=(n,)) for n in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("admitted") == 1
    assert outcomes.count("already_used") == 11
    assert OutboundCheckin.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_outbox_delivers_each_scan_once_through_failures(mock_api):
    apply_ticket_versions([(f"T{n}", "E", None, "valid", 1) for n in range(40)])
    for n in range(40):
        scan_ticket(f"T{n}", f"scan-{n}")
    faults(mock_api, p_500=0.35, p_429=0.1, retry_after=0.001)
    client = client_for(mock_api, policy=RetryPolicy(max_attempts=1))
    for _ in range(60):
        flush_outbox(client)
        if not OutboundCheckin.objects.filter(status="pending").exists():
            break
        OutboundCheckin.objects.filter(status="pending").update(next_attempt_at="2000-01-01T00:00:00Z")
    assert OutboundCheckin.objects.filter(status="sent").count() == 40
    assert stats(mock_api)["side_effects"] == 40


@pytest.mark.django_db(transaction=True)
def test_outbox_keeps_messages_while_breaker_is_open_without_burning_attempts(mock_api):
    apply_ticket_versions([("T1", "E", None, "valid", 1)])
    scan_ticket("T1", "scan-1")
    faults(mock_api, outage=True)
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=100)
    breaker.record_failure()
    result = flush_outbox(client_for(mock_api, breaker=breaker, policy=RetryPolicy(max_attempts=1)))
    assert result.stopped_by
    message = OutboundCheckin.objects.get()
    assert (message.status, message.attempts) == ("pending", 0)


@pytest.mark.django_db(transaction=True)
def test_outbox_gives_up_into_dead_letters_and_keeps_the_message(mock_api):
    apply_ticket_versions([("T1", "E", None, "valid", 1)])
    scan_ticket("T1", "scan-x")
    OutboundCheckin.objects.update(attempts=7)
    faults(mock_api, p_500=1.0)
    flush_outbox(client_for(mock_api, policy=RetryPolicy(max_attempts=1)))
    assert OutboundCheckin.objects.get().status == "dead"
    assert DeadLetter.objects.filter(kind="checkin").count() == 1


@pytest.mark.django_db
def test_metrics_merge_across_workers(mock_api):
    one, two = client_for(mock_api), client_for(mock_api)
    one.fetch_tickets("", 1)
    two.fetch_tickets("", 1)
    save_stats(one.stats)
    save_stats(two.stats)
    metric = ConnectorMetric.objects.get()
    assert metric.requests == 2
    assert sum(metric.latency_buckets.values()) == 2


@pytest.mark.django_db
def test_scan_api_requires_the_operator_token_when_one_is_set(settings):
    settings.ENCORE_OPERATOR_TOKEN = "op-token"
    apply_ticket_versions([("T1", "E", None, "valid", 1)])
    api = APIClient()
    data = {"ticket": "T1", "scan_id": str(uuid.uuid4())}
    assert api.post("/api/v1/scans", data, format="json").status_code == 401
    assert (
        api.post("/api/v1/scans", data, format="json", HTTP_AUTHORIZATION="Bearer wrong").status_code == 401
    )
    assert (
        api.post("/api/v1/scans", data, format="json", HTTP_AUTHORIZATION="Bearer op-token").status_code
        == 200
    )


@pytest.mark.django_db
def test_writes_are_closed_outside_debug_when_no_token_is_set(settings):
    settings.ENCORE_OPERATOR_TOKEN = ""
    settings.DEBUG = False
    response = APIClient().post("/api/v1/scans", {"ticket": "T", "scan_id": "s"}, format="json")
    assert response.status_code == 401


@pytest.mark.django_db
def test_availability_serves_cache_and_supports_etag():
    from django.core.cache import cache

    cache.clear()
    apply_ticket_versions([("T1", "EV", None, "valid", 1)])
    api = APIClient()
    first = api.get("/api/v1/events/EV/availability")
    assert first.json() == {"issued": 1, "valid": 1, "checked_in": 0}
    assert first["X-Cache"] == "miss"
    second = api.get("/api/v1/events/EV/availability")
    assert second["X-Cache"] == "hit"
    assert api.get("/api/v1/events/EV/availability", HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304


@pytest.mark.django_db
def test_import_api_cannot_read_outside_the_source_folder(settings, tmp_path):
    settings.ENCORE_OPERATOR_TOKEN = "op"
    settings.ENCORE_IMPORT_SOURCE_DIR = tmp_path
    (tmp_path.parent / "secret.csv").write_text("x")
    response = APIClient().post(
        "/api/v1/imports/runs", {"source": "../secret.csv"}, format="json", HTTP_AUTHORIZATION="Bearer op"
    )
    assert response.status_code == 400


def test_repeated_timeouts_open_the_breaker(mock_api):
    faults(mock_api, p_timeout=1.0, timeout_seconds=2)
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=60)
    client = client_for(mock_api, breaker=breaker, policy=RetryPolicy(max_attempts=5, base_delay=0.001))
    with pytest.raises(CircuitOpen):
        client.fetch_tickets("")
    assert client.stats.counts["timeouts"] == 2


def test_every_request_lands_in_exactly_one_outcome(mock_api):
    faults(mock_api, p_429=0.2, p_500=0.2, retry_after=0.001)
    client = client_for(mock_api, policy=RetryPolicy(max_attempts=15, base_delay=0.001, max_delay=0.01))
    for _ in range(10):
        client.fetch_tickets("", 5)
    faults(mock_api)
    with pytest.raises(PermanentError):
        client.post_checkin("", "key")
    counts = client.stats.counts
    outcomes = (
        counts["successes"]
        + counts["rate_limited"]
        + counts["server_errors"]
        + counts["timeouts"]
        + counts["client_errors"]
    )
    assert outcomes == counts["requests"]
    assert counts["client_errors"] == 1


@pytest.mark.django_db(transaction=True)
def test_health_reports_cursor_lag_and_breaker_detail(mock_api, platform_client):
    assert platform_client.get("/api/v1/connector/health").json()["cursor_lag"] is None, "Unknown is not zero"
    sync_tickets(client_for(mock_api), max_pages=2, limit=100)
    health = platform_client.get("/api/v1/connector/health").json()
    feed = stats(mock_api)["feed_size"]
    assert health["feed_head"] == feed
    assert health["cursor_lag"] == feed - 200


@pytest.mark.django_db
def test_fault_control_needs_operator_and_refuses_a_real_api(settings):
    settings.ENCORE_OPERATOR_TOKEN = "op"
    api = APIClient()
    assert api.post("/api/v1/connector/faults", {"outage": True}, format="json").status_code == 401
    settings.TICKETING_IS_MOCK = False
    reply = api.post(
        "/api/v1/connector/faults", {"outage": True}, format="json", HTTP_AUTHORIZATION="Bearer op"
    )
    assert reply.status_code == 404


@pytest.mark.django_db
def test_dead_letter_list_and_resolve(settings):
    settings.ENCORE_OPERATOR_TOKEN = "op"
    handle_webhook("bad", {"ticket": {"id": 5}})
    api = APIClient()
    assert api.get("/api/v1/connector/dead-letters").status_code == 401
    rows = api.get("/api/v1/connector/dead-letters", HTTP_AUTHORIZATION="Bearer op").json()
    assert rows[0]["kind"] == "webhook"
    url = f"/api/v1/connector/dead-letters/{rows[0]['id']}/resolve"
    assert api.post(url).status_code == 401
    assert api.post(url, HTTP_AUTHORIZATION="Bearer op").status_code == 200
    assert api.post(url, HTTP_AUTHORIZATION="Bearer op").status_code == 404
    assert api.get("/api/v1/connector/dead-letters", HTTP_AUTHORIZATION="Bearer op").json() == []
    assert DeadLetter.objects.count() == 1


@pytest.mark.django_db
def test_debug_machine_refuses_writes_from_another_website(settings):
    settings.DEBUG = True
    settings.ENCORE_OPERATOR_TOKEN = ""
    api = APIClient()
    data = {"ticket": "T", "scan_id": "s"}
    assert (
        api.post("/api/v1/scans", data, format="json", HTTP_ORIGIN="http://evil.example").status_code == 401
    )
    assert api.post("/api/v1/scans", data, format="json", HTTP_ORIGIN="http://testserver").status_code == 404
    assert api.post("/api/v1/scans", data, format="json").status_code == 404


@pytest.mark.django_db
def test_form_posts_are_not_parsed(settings):
    settings.DEBUG = True
    settings.ENCORE_OPERATOR_TOKEN = ""
    reply = APIClient().post("/api/v1/scans", {"ticket": "T", "scan_id": "s"}, format="multipart")
    assert reply.status_code == 415


@pytest.mark.django_db
def test_nul_byte_in_source_name_is_a_clean_refusal(settings, tmp_path):
    settings.ENCORE_OPERATOR_TOKEN = "op"
    settings.ENCORE_IMPORT_SOURCE_DIR = tmp_path
    reply = APIClient().post(
        "/api/v1/imports/runs", {"source": "a\x00b"}, format="json", HTTP_AUTHORIZATION="Bearer op"
    )
    assert reply.status_code == 400


def test_missing_source_folder_is_an_empty_list(settings, tmp_path, platform_client):
    settings.ENCORE_IMPORT_SOURCE_DIR = tmp_path / "absent"
    assert platform_client.get("/api/v1/imports/sources").json() == []


@pytest.mark.django_db
def test_security_headers_outside_debug(settings):
    settings.DEBUG = False
    settings.ALLOWED_HOSTS = ["testserver"]
    reply = APIClient().get("/load-results")
    assert reply["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in reply["Content-Security-Policy"]
    assert "unsafe-inline" not in reply["Content-Security-Policy"].split("script-src")[1].split(";")[0]


def test_stand_in_refuses_unknown_fault_settings(mock_api):
    assert httpx.post(f"{mock_api}/_faults", json={"nonsense": 1}).status_code == 400


@pytest.mark.django_db
def test_event_page_shows_numbers_sets_no_cookie_and_handles_unknown_events():
    from django.core.cache import cache

    cache.clear()
    apply_ticket_versions([("T1", "SHOW", None, "valid", 1)])
    client = APIClient()
    page = client.get("/events/SHOW")
    assert page.status_code == 200
    assert "Tickets issued" in page.content.decode()
    assert not page.cookies
    empty = client.get("/events/NOTHING%3Cscript%3E")
    assert empty.status_code == 200
    assert "<script>" not in empty.content.decode()


# ---------------------------------------------------------------- the scheduled tasks Celery beat runs


@pytest.mark.django_db(transaction=True)
def test_beat_sync_task_pulls_the_feed_and_publishes_breaker_and_metrics(mock_api, settings):
    from ticketing.models import ConnectorState
    from ticketing.tasks import sync_task

    settings.TICKETING_BASE_URL = mock_api
    result = sync_task()
    assert result["caught_up"] and result["applied"] >= Ticket.objects.count() == 1000
    assert ConnectorState.objects.get(name="ticketing").state == "closed"
    assert ConnectorMetric.objects.exists(), "Every task run publishes its call metrics"


@pytest.mark.django_db(transaction=True)
def test_beat_flush_task_delivers_pending_check_ins_once(mock_api, settings):
    from ticketing.tasks import flush_task

    settings.TICKETING_BASE_URL = mock_api
    apply_ticket_versions([("T1", "E", None, "valid", 1), ("T2", "E", None, "valid", 1)])
    scan_ticket("T1", "scan-1")
    scan_ticket("T2", "scan-2")
    result = flush_task()
    assert result["sent"] == 2
    assert OutboundCheckin.objects.filter(status="sent").count() == 2
    assert flush_task()["sent"] == 0, "A second run finds nothing left to send"
    assert stats(mock_api)["side_effects"] == 2
