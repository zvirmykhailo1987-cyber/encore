# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Guarantees an independent review found broken or unproven, each pinned by a test that fails without its fix."""

import datetime as dt
import os
import sys
from pathlib import Path

import pytest
from django.test import Client
from django.utils import timezone

from crm.erasure import erase_subject
from crm.models import Contact
from imports.models import ContactChange, ImportRun, QuarantineRow
from imports.pipeline import RunBusy, _claim, execute_run, rollback_run
from imports.reconcile import reconcile_run
from ticketing.models import ConnectorState, DeadLetter, EventOwner, Ticket, WebhookEvent
from ticketing.security import sign_webhook, webhook_is_authentic
from ticketing.services import _load_breaker, _save_breaker, apply_ticket_versions, parse_ticket

pytestmark = pytest.mark.django_db

HEADER = "CUST_ID,EMAIL_ADDR,MOBILE,FNAME,LNAME,TOWN,OPT_IN,CREATED\n"
CARD = "4111 1111 1111 1111"


def write(tmp_path: Path, body: str, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes((HEADER + body).encode("utf-8", "surrogateescape"))
    return path


def run_file(path: Path, organizer: str = "demo", batch_size: int = 50) -> ImportRun:
    run = ImportRun.objects.create(source_path=str(path), organizer=organizer)
    return execute_run(run.pk, batch_size=batch_size)


# ---------------------------------------------------------------- rollback


def test_rollback_refuses_while_a_later_paused_run_holds_written_rows(tmp_path):
    first = run_file(write(tmp_path, "1,a@x.io,,Anna,,,,\n", "a.csv"))
    later = ImportRun.objects.create(
        source_path=str(write(tmp_path, "2,b@x.io,,B,,,,\n3,c@x.io,,C,,,,\n", "b.csv"))
    )
    execute_run(
        later.pk,
        batch_size=1,
        after_batch=lambda n: ImportRun.objects.filter(pk=later.pk).update(pause_requested=True),
    )
    later.refresh_from_db()
    assert later.status == "paused" and later.batches_committed > 0
    with pytest.raises(RunBusy):
        rollback_run(first.pk)
    assert Contact.objects.filter(email="b@x.io").exists(), "The paused run's rows are untouched"


def test_a_failed_run_that_wrote_rows_can_be_rolled_back(tmp_path):
    run = run_file(write(tmp_path, "1,a@x.io,,Anna,,,,\n", "a.csv"))
    ImportRun.objects.filter(pk=run.pk).update(status=ImportRun.Status.FAILED)
    rollback_run(run.pk)
    assert not Contact.objects.exists()


def test_rollback_does_not_bring_back_a_person_erased_since(tmp_path):
    run_file(write(tmp_path, "1,old@x.io,,Old,,,,\n", "a.csv"))
    second = run_file(write(tmp_path, "1,new@x.io,,New,,,,\n", "b.csv"))
    assert Contact.objects.get().email == "new@x.io"
    erase_subject("demo", "old@x.io")
    assert not ContactChange.objects.filter(before__email="old@x.io").exists(), "Snapshots are erased too"
    rollback_run(second.pk)
    assert not Contact.objects.filter(email="old@x.io").exists()


# ---------------------------------------------------------------- one contact, one object


def test_a_known_id_changed_in_the_batch_where_another_id_merges_into_it_keeps_its_change(tmp_path):
    run_file(write(tmp_path, "1,x@x.io,,Anna,,,,\n", "a.csv"))
    # Row 2 is a new legacy id with the same email: it merges into the contact. Row 1 then renames that contact.
    second = run_file(write(tmp_path, "2,x@x.io,,,,,,\n1,x@x.io,,Hanna,,,,\n", "b.csv"))
    assert (second.rows_merged, second.rows_updated) == (1, 1)
    assert Contact.objects.get().first_name == "Hanna"
    assert second.reconciliation["balanced"]


# ---------------------------------------------------------------- card numbers in rows broken for another reason


def test_a_card_number_in_a_row_with_the_wrong_column_count_is_not_kept(tmp_path):
    run = run_file(write(tmp_path, f"1,a@x.io,,A,,,,,extra {CARD}\n", "a.csv"))
    row = QuarantineRow.objects.get(run=run)
    assert row.reason == "column_count"
    assert row.raw == {} and "4111" not in row.detail


def test_a_card_number_in_a_line_that_is_not_json_is_not_kept(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_text('{"CUST_ID": "1", "notes": "' + CARD + '"\n', encoding="utf-8")
    run = ImportRun.objects.create(source_path=str(path), source_format="jsonl")
    execute_run(run.pk)
    row = QuarantineRow.objects.get(run=run)
    assert row.reason == "bad_json"
    assert row.raw == {}


# ---------------------------------------------------------------- erasure reaches every copy


def test_erasure_clears_broken_rows_of_every_shape_and_spacing(tmp_path):
    run = run_file(
        write(
            tmp_path, "1,a@x.io,,A,,,,,extra\n2, A@X.io ,,A,,,,not-a-date\n3,b@x.io,,B,,,,,extra\n", "a.csv"
        )
    )
    assert QuarantineRow.objects.filter(run=run).count() == 3
    erase_subject("demo", "a@x.io")
    texts = [str(row.raw) + row.detail for row in QuarantineRow.objects.filter(run=run)]
    assert not any("a@x.io" in text.lower() for text in texts)
    assert any("b@x.io" in text for text in texts), "Another person's broken row stays"


def test_erasure_clears_ticket_holders_of_that_organizers_events_only():
    EventOwner.objects.create(event_external_id="FEST", organizer="demo")
    apply_ticket_versions([("T1", "FEST", "a@x.io", "valid", 1), ("T2", "OTHER", "a@x.io", "valid", 1)])
    WebhookEvent.objects.create(
        event_id="w1", payload={"ticket": {"id": "T1", "event": "FEST", "email": "A@x.io"}}
    )
    result = erase_subject("demo", "a@x.io")
    assert result["ticket_holders_cleared"] == 1
    assert Ticket.objects.get(external_id="T1").holder_email is None
    assert Ticket.objects.get(external_id="T2").holder_email == "a@x.io", "Another organizer's ticket stays"
    assert "a@x.io" not in str(WebhookEvent.objects.get().payload).lower()


# ---------------------------------------------------------------- one odd document does not stop a run


def test_a_mongo_document_with_an_unreadable_date_is_quarantined_and_the_run_completes(tmp_path):
    path = tmp_path / "users.mongoexport.jsonl"
    path.write_text(
        '{"_id": "1", "emails": [{"address": "a@x.io"}], "createdAt": {"$date": {"$numberLong": "abc"}}}\n'
        '{"_id": "2", "emails": [{"address": "b@x.io"}]}\n',
        encoding="utf-8",
    )
    run = ImportRun.objects.create(source_path=str(path), source_format="mongo")
    run = execute_run(run.pk)
    assert run.status == "completed"
    assert (run.rows_created, run.rows_quarantined) == (1, 1)
    assert QuarantineRow.objects.get(run=run).reason == "bad_value"


# ---------------------------------------------------------------- reconciliation reads the stored fields


def test_reconciliation_catches_a_field_that_was_not_saved(tmp_path):
    run = run_file(write(tmp_path, "1,a@x.io,+390612345678,Anna,,Rome,,\n", "a.csv"))
    assert run.reconciliation["balanced"]
    Contact.objects.update(city="Milan")  # as if the import had dropped this column from its update
    report = reconcile_run(ImportRun.objects.get(pk=run.pk))
    assert report["field_mismatches"] == 1 and not report["balanced"]


# ---------------------------------------------------------------- the importer reads only what was named


@pytest.mark.skipif(
    sys.platform == "win32" and not os.environ.get("CI"), reason="Symlinks need privileges on Windows"
)
def test_a_source_swapped_for_a_link_after_the_start_is_refused(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-other.csv"
    outside.write_text(HEADER + "9,them@x.io,,,,,,\n", encoding="utf-8")
    path = write(tmp_path, "1,a@x.io,,,,,,\n", "a.csv")
    run = ImportRun.objects.create(source_path=str(path))
    path.unlink()
    path.symlink_to(outside)
    run = execute_run(run.pk)
    assert run.status == "failed" and "link" in run.error
    assert not Contact.objects.exists()


# ---------------------------------------------------------------- a busy organizer is waited for, not forgotten


def test_a_second_import_for_a_busy_organizer_waits_and_says_why(tmp_path):
    from imports.tasks import run_import

    first = ImportRun.objects.create(source_path=str(write(tmp_path, "1,a@x.io,,,,,,\n", "a.csv")))
    _claim(first.pk)
    second = ImportRun.objects.create(source_path=str(write(tmp_path, "2,b@x.io,,,,,,\n", "b.csv")))
    # Eagerly, Celery runs every retry at once; a worker would space them 30 seconds apart.
    run_import.apply(args=[second.pk])
    second.refresh_from_db()
    assert second.status == "pending"
    assert "another import for this organizer" in second.error


# ---------------------------------------------------------------- the connector


def test_a_webhook_signed_with_nan_or_infinity_is_refused():
    for stamp in ("nan", "inf", "-1", "1e9", ""):
        signature = sign_webhook("local-demo-webhook-secret", stamp, b"{}")
        assert not webhook_is_authentic(b"{}", stamp, signature), stamp


def test_feed_items_longer_than_the_columns_are_dead_lettered_not_fatal():
    assert parse_ticket({"id": "T" * 65, "event": "E", "version": 1, "status": "valid"}) is None
    assert (
        parse_ticket({"id": "T1", "event": "E", "version": 1, "status": "valid", "email": "a" * 255}) is None
    )
    assert parse_ticket({"id": "T1", "event": "E", "version": 1, "status": "valid"}) is not None


def test_a_webhook_with_an_overlong_id_is_refused_as_malformed(settings):
    import json
    import time

    body = json.dumps({"id": "x" * 65, "ticket": {}}).encode()
    stamp = str(int(time.time()))
    reply = Client().post(
        "/api/v1/webhooks/ticketing",
        body,
        content_type="application/json",
        headers={
            "X-Timestamp": stamp,
            "X-Signature": sign_webhook(settings.TICKETING_WEBHOOK_SECRET, stamp, body),
        },
    )
    assert reply.status_code == 400
    assert not WebhookEvent.objects.exists()


def test_a_task_that_started_before_an_outage_does_not_close_the_breaker_again():
    sync_view, sync_loaded = _load_breaker()  # a sync task starts and sees "closed"
    ConnectorState.objects.filter(name="ticketing").update(state="open", opened_at=1.0, times_opened=1)
    sync_view.consecutive_failures = 0  # the sync task saw nothing wrong in its own calls
    _save_breaker(sync_view, sync_loaded)
    assert ConnectorState.objects.get(name="ticketing").state == "open"


def test_fresh_failures_still_open_the_breaker_when_another_task_saved_first():
    view, loaded = _load_breaker()
    ConnectorState.objects.filter(name="ticketing").update(consecutive_failures=2)
    view.state, view.opened_at, view.times_opened = "open", 5.0, 1
    _save_breaker(view, loaded)
    assert ConnectorState.objects.get(name="ticketing").state == "open"


def test_old_webhook_events_are_purged():
    from ticketing.tasks import purge_webhook_events

    old = WebhookEvent.objects.create(event_id="old", payload={})
    WebhookEvent.objects.filter(pk=old.pk).update(received_at=timezone.now() - dt.timedelta(days=31))
    WebhookEvent.objects.create(event_id="new", payload={})
    assert purge_webhook_events() == 1
    assert list(WebhookEvent.objects.values_list("event_id", flat=True)) == ["new"]


def test_a_sync_page_announcing_more_without_a_new_cursor_stops_instead_of_looping():
    from ticketing.services import sync_tickets

    class Stuck:
        calls = 0

        def fetch_tickets(self, cursor, limit):
            self.calls += 1
            return {"items": [], "has_more": True, "next_cursor": None}

    client = Stuck()
    result = sync_tickets(client, max_pages=50)  # type: ignore[arg-type]
    assert client.calls == 1 and result.stopped_by == "no new cursor"
    assert DeadLetter.objects.filter(kind="ticket_page").exists()
