"""What a multi-tenant live-events platform needs beyond a clean import: tenants, erasure, offline gates, operations."""

import datetime as dt
import json
import logging
import secrets
from pathlib import Path

import httpx
import pytest
from django.core.management import call_command
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from crm.erasure import erase_subject, export_ledger, replay_erasures
from crm.models import Contact, ContactAlias, ErasedSubject
from imports.cutover import cutover_report
from imports.models import ImportRun, QuarantineRow
from imports.pipeline import RunBusy, _claim, execute_run, process_batch
from imports.reconcile import reconcile_run
from imports.source import Record
from imports.tasks import reap_stale
from ticketing.client import ConnectorError, _retry_after
from ticketing.models import OutboundCheckin, ScanConflict, Ticket
from ticketing.services import flush_outbox

pytestmark = pytest.mark.django_db(transaction=True)

HEADER = "CUST_ID,EMAIL_ADDR,MOBILE,FNAME,LNAME,TOWN,OPT_IN,CREATED\n"
JSON_TYPE = "application/json"
TOKEN = secrets.token_urlsafe(24)  # random per run, never a fixed credential


@pytest.fixture(autouse=True)
def operator_token(settings):
    settings.ENCORE_OPERATOR_TOKEN = TOKEN


def operator() -> Client:
    return Client(headers={"Authorization": f"Bearer {TOKEN}"})


def csv_file(tmp_path: Path, body: str, name: str = "src.csv") -> Path:
    path = tmp_path / name
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def run_for(path: Path, organizer: str, **kwargs) -> ImportRun:
    run = ImportRun.objects.create(source_path=str(path), organizer=organizer, **kwargs)
    return execute_run(run.pk, batch_size=2)


# ---------------------------------------------------------------- organizers


def test_same_person_at_two_organizers_stays_two_separate_contacts(tmp_path):
    path = csv_file(tmp_path, "1,anna@example.test,,Anna,,,,\n")
    first = run_for(path, "festival-a")
    second = run_for(path, "club-b")
    assert (first.rows_created, second.rows_created, second.rows_merged) == (1, 1, 0)
    assert Contact.objects.filter(email="anna@example.test").count() == 2
    assert ContactAlias.objects.filter(src_uid="legacy:1").count() == 2
    assert first.reconciliation["balanced"] and second.reconciliation["balanced"]


def test_a_rerun_for_one_organizer_does_not_touch_another(tmp_path):
    run_for(csv_file(tmp_path, "1,anna@example.test,,Anna,,,,\n"), "festival-a")
    run_for(csv_file(tmp_path, "1,anna@example.test,,Anna,,,,\n", "b.csv"), "club-b")
    changed = run_for(csv_file(tmp_path, "1,anna@example.test,,Hanna,,,,\n", "c.csv"), "festival-a")
    assert changed.rows_updated == 1
    assert Contact.objects.get(organizer="festival-a").first_name == "Hanna"
    assert Contact.objects.get(organizer="club-b").first_name == "Anna"


def test_writers_are_limited_per_organizer_not_globally(tmp_path):
    path = csv_file(tmp_path, "1,a@example.test,,,,,,\n")
    _claim(ImportRun.objects.create(source_path=str(path), organizer="festival-a").pk)
    _claim(ImportRun.objects.create(source_path=str(path), organizer="club-b").pk)
    same = ImportRun.objects.create(source_path=str(path), organizer="festival-a")
    with pytest.raises(RunBusy):
        _claim(same.pk)


def test_api_refuses_a_malformed_organizer(settings, tmp_path):
    settings.ENCORE_IMPORT_SOURCE_DIR = tmp_path
    csv_file(tmp_path, "1,a@example.test,,,,,,\n")
    reply = operator().post(
        "/api/v1/imports/runs", {"source": "src.csv", "organizer": "../other"}, content_type=JSON_TYPE
    )
    assert reply.status_code == 400


# ---------------------------------------------------------------- erasure during a migration


def test_erased_person_is_not_recreated_by_a_rerun_and_other_organizer_stays(tmp_path):
    path = csv_file(tmp_path, "1,anna@example.test,,Anna,,,,\n2,bo@example.test,,Bo,,,,\n")
    first = run_for(path, "festival-a")
    run_for(path, "club-b")
    assert erase_subject("festival-a", "ANNA@example.test ")["contacts_removed"] == 1
    assert not Contact.objects.filter(organizer="festival-a", email="anna@example.test").exists()
    assert Contact.objects.filter(organizer="club-b", email="anna@example.test").exists()

    report = reconcile_run(ImportRun.objects.get(pk=first.pk))
    assert report["balanced"], report
    assert report["erased_rows"] == 1

    rerun = run_for(csv_file(tmp_path, "1,anna@example.test,,Anna,,,,\n", "again.csv"), "festival-a")
    assert (rerun.rows_created, rerun.rows_quarantined) == (0, 1)
    row = QuarantineRow.objects.get(run=rerun)
    assert (row.reason, row.raw) == ("erased_subject", {})
    assert not Contact.objects.filter(organizer="festival-a", email="anna@example.test").exists()


def test_erasure_also_clears_the_person_from_quarantine(tmp_path):
    path = csv_file(tmp_path, "1,anna@example.test,bad-phone,Anna,,,,\n")
    run = run_for(path, "festival-a")
    assert QuarantineRow.objects.get(run=run).raw["EMAIL_ADDR"] == "anna@example.test"
    assert erase_subject("festival-a", "anna@example.test")["quarantine_rows_cleared"] == 1
    assert QuarantineRow.objects.get(run=run).raw == {}


def test_erased_people_stay_erased_after_a_restore_brings_them_back(tmp_path):
    path = csv_file(tmp_path, "1,anna@example.test,,Anna,,,,\n2,bo@example.test,,Bo,,,,\n")
    run_for(path, "festival-a")
    erase_subject("festival-a", "anna@example.test")
    ledger = export_ledger()
    # What a restore from yesterday's backup does: the person is back, and the ledger forgot them.
    ErasedSubject.objects.all().delete()
    Contact.objects.create(organizer="festival-a", email="anna@example.test")
    assert replay_erasures(ledger)["contacts_removed"] == 1
    assert list(Contact.objects.values_list("email", flat=True)) == ["bo@example.test"]


def test_consent_proof_travels_with_the_contact_and_gaps_are_reported(tmp_path):
    path = tmp_path / "consent.csv"
    path.write_text(
        "CUST_ID,EMAIL_ADDR,OPT_IN,OPT_IN_AT,OPT_IN_TEXT\n"
        "1,a@example.test,Y,2023-05-01,Signup form v3\n"
        "2,b@example.test,Y,,\n"
        "3,c@example.test,,,\n",
        encoding="utf-8",
    )
    run_for(path, "festival-a")
    proven = Contact.objects.get(email="a@example.test")
    assert proven.consent_recorded_at == dt.date(2023, 5, 1)
    assert proven.consent_wording == "Signup form v3"
    assert Contact.objects.get(email="c@example.test").marketing_opt_in is None
    assert cutover_report("festival-a")["opt_ins_without_proof"] == 1


def test_a_card_number_in_any_column_is_quarantined_with_no_content_kept(tmp_path):
    path = tmp_path / "notes.csv"
    path.write_text(
        "CUST_ID,EMAIL_ADDR,NOTES\n"
        "1,a@example.test,paid with 4111 1111 1111 1111 exp 12/29\n"
        "2,b@example.test,order 4111111111111112 is not a card\n"
        "3,c@example.test,+390000000000\n",
        encoding="utf-8",
    )
    run = run_for(path, "festival-a")
    row = QuarantineRow.objects.get(run=run)
    assert (row.row_number, row.reason, row.raw, row.detail) == (1, "possible_card_number", {}, "")
    assert run.rows_created == 2
    assert run.reconciliation["balanced"], run.reconciliation


def test_rows_gone_from_the_source_are_reported_and_not_deleted(tmp_path):
    run_for(csv_file(tmp_path, "1,a@example.test,,,,,,\n2,b@example.test,,,,,,\n"), "festival-a")
    second = run_for(csv_file(tmp_path, "1,a@example.test,,,,,,\n", "later.csv"), "festival-a")
    assert second.reconciliation["in_database_not_in_this_file"] == 1
    assert second.reconciliation["not_in_this_file_sample"] == ["legacy:2"]
    assert second.reconciliation["balanced"]
    assert Contact.objects.filter(organizer="festival-a").count() == 2


# ---------------------------------------------------------------- a MongoDB source


MONGO_ROWS = [
    {
        "_id": {"$oid": "65f0c1a2b3c4d5e6f7a8b9c0"},
        "emails": [{"address": "Anna@Example.test", "verified": True}],
        "profile": {"firstName": "Anna", "marketingOptIn": True},
        "createdAt": {"$date": "2024-03-01T10:00:00.000Z"},
        "services": {"password": {"bcrypt": "must-never-be-read"}},
    },
    {
        "_id": "plain-string-id",
        "profile": {"phone": "+390000000000"},
        "legacyFlag": 1,
        "createdAt": {"$date": {"$numberLong": "-86400000"}},
    },
    {"_id": {"$oid": "65f0c1a2b3c4d5e6f7a8b9c1"}, "emails": []},
]


def test_mongoexport_source_handles_types_drift_and_missing_contact_channel(tmp_path):
    path = tmp_path / "users.mongoexport.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in MONGO_ROWS) + "\nnot json\n", encoding="utf-8")
    run = run_for(path, "festival-a", source_format="mongo")
    assert run.status == ImportRun.Status.COMPLETED
    assert (run.rows_read, run.rows_created, run.rows_quarantined) == (4, 2, 2)
    contact = Contact.objects.get(email="anna@example.test")
    assert contact.marketing_opt_in is True
    assert contact.legacy_created_at == dt.date(2024, 3, 1)
    assert ContactAlias.objects.filter(src_uid="legacy:65f0c1a2b3c4d5e6f7a8b9c0").exists()
    assert sorted(run.unmapped_columns) == ["legacyFlag", "services"]
    assert Contact.objects.get(phone="+390000000000").legacy_created_at == dt.date(1969, 12, 31)
    assert run.reconciliation["balanced"], run.reconciliation
    again = run_for(path, "festival-a", source_format="mongo")
    assert (again.rows_created, again.rows_skipped) == (0, 2)


# ---------------------------------------------------------------- outbox delivery


class RecordingClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.inside_transaction: list[bool] = []

    def post_checkin(self, ticket: str, key: str) -> None:
        self.calls.append((ticket, key))
        self.inside_transaction.append(connection.in_atomic_block)
        if self.error:
            raise self.error


def test_outbox_holds_no_transaction_during_the_http_call():
    OutboundCheckin.objects.create(ticket_external_id="T1", idempotency_key="scan-1")
    client = RecordingClient()
    assert flush_outbox(client).sent == 1
    assert client.inside_transaction == [False]


def test_worker_dying_mid_call_leaves_a_lease_that_expires_and_sends_the_same_key():
    message = OutboundCheckin.objects.create(ticket_external_id="T1", idempotency_key="scan-1")

    class Died(Exception):
        pass

    client = RecordingClient(Died())
    with pytest.raises(Died):
        flush_outbox(client)
    message.refresh_from_db()
    assert message.status == "pending"
    assert message.attempts == 1
    assert message.next_attempt_at > timezone.now()

    second = RecordingClient()
    assert flush_outbox(second).sent == 0  # Still leased.
    OutboundCheckin.objects.filter(pk=message.pk).update(next_attempt_at=timezone.now())
    assert flush_outbox(second).sent == 1
    assert second.calls == [("T1", "scan-1")]


def test_outbox_retry_schedule_replaces_the_lease():
    message = OutboundCheckin.objects.create(ticket_external_id="T1", idempotency_key="scan-1")
    assert flush_outbox(RecordingClient(ConnectorError("boom"))).retried == 1
    message.refresh_from_db()
    assert message.last_error == "boom"
    assert message.next_attempt_at <= timezone.now() + dt.timedelta(seconds=3)


# ---------------------------------------------------------------- gates without a network


def upload(device: str, *scans: dict):
    return operator().post(
        "/api/v1/scans/offline", {"device": device, "scans": list(scans)}, content_type=JSON_TYPE
    )


def offline_scan(ticket: str, scan_id: str, when: dt.datetime | None = None) -> dict:
    return {"ticket": ticket, "scan_id": scan_id, "scanned_at": (when or timezone.now()).isoformat()}


def test_manifest_gives_a_gate_what_it_needs_and_no_personal_data():
    Ticket.objects.create(external_id="T1", event_id_external="E1", holder_email="anna@example.test")
    Ticket.objects.create(
        external_id="T2", event_id_external="E1", status="cancelled", checked_in_at=timezone.now()
    )
    Ticket.objects.create(external_id="T3", event_id_external="OTHER")
    reply = operator().get("/api/v1/events/E1/manifest")
    assert reply.status_code == 200
    assert "anna" not in reply.content.decode()
    assert reply.json()["tickets"] == [
        {"ticket": "T1", "status": "valid", "used": False},
        {"ticket": "T2", "status": "cancelled", "used": True},
    ]


def test_upload_keeps_the_scan_time_writes_one_outbox_message_and_can_be_repeated():
    Ticket.objects.create(external_id="T1", event_id_external="E1")
    when = timezone.now() - dt.timedelta(hours=1)
    scan = offline_scan("T1", "gate-1-0001", when)
    assert upload("gate-1", scan).json()["results"] == [{"scan_id": "gate-1-0001", "result": "accepted"}]
    assert upload("gate-1", scan).json()["results"] == [
        {"scan_id": "gate-1-0001", "result": "duplicate_upload"}
    ]
    ticket = Ticket.objects.get(external_id="T1")
    assert ticket.checked_in_at == when
    assert OutboundCheckin.objects.filter(idempotency_key="gate-1-0001").count() == 1


def test_two_offline_gates_admitting_one_ticket_become_a_conflict_to_review():
    Ticket.objects.create(external_id="T1", event_id_external="E1")
    upload("gate-1", offline_scan("T1", "gate-1-0001"))
    results = upload("gate-2", offline_scan("T1", "gate-2-0001")).json()["results"]
    assert results == [{"scan_id": "gate-2-0001", "result": "conflict"}]
    conflict = ScanConflict.objects.get()
    assert (conflict.device, conflict.reason, conflict.first_scan_id) == (
        "gate-2",
        "already_used",
        "gate-1-0001",
    )
    assert OutboundCheckin.objects.count() == 1
    upload("gate-2", offline_scan("T1", "gate-2-0001"))
    assert ScanConflict.objects.count() == 1


def test_upload_rejects_future_times_and_junk_without_losing_the_good_scans():
    Ticket.objects.create(external_id="T1", event_id_external="E1")
    future = timezone.now() + dt.timedelta(hours=2)
    results = upload(
        "gate-1", offline_scan("T1", "late", future), {"ticket": 5}, "junk", offline_scan("T1", "good")
    ).json()["results"]
    assert [row["result"] for row in results] == ["rejected", "rejected", "rejected", "accepted"]


def test_unknown_ticket_scanned_offline_is_kept_for_review():
    assert upload("gate-1", offline_scan("NOPE", "s1")).json()["results"][0]["result"] == "conflict"
    assert ScanConflict.objects.get().reason == "unknown_ticket"


def test_ticket_refunded_after_the_device_synced_becomes_a_conflict_not_a_normal_entry():
    Ticket.objects.create(external_id="T1", event_id_external="E1", status="refunded")
    assert upload("gate-1", offline_scan("T1", "s1")).json()["results"][0]["result"] == "conflict"
    assert ScanConflict.objects.get().reason == "ticket_refunded"
    assert not OutboundCheckin.objects.exists()
    assert Ticket.objects.get(external_id="T1").checked_in_at is None


def test_one_scan_id_used_for_two_tickets_admits_nobody_new():
    Ticket.objects.create(external_id="T1", event_id_external="E1")
    Ticket.objects.create(external_id="T2", event_id_external="E1")
    assert upload("gate-1", offline_scan("T1", "same")).json()["results"][0]["result"] == "accepted"
    assert upload("gate-1", offline_scan("T2", "same")).json()["results"][0]["result"] == "rejected"
    assert Ticket.objects.get(external_id="T2").checked_in_at is None
    live = operator().post("/api/v1/scans", {"ticket": "T2", "scan_id": "same"}, content_type=JSON_TYPE)
    assert (live.status_code, live.json()["outcome"]) == (422, "scan_id_reused")
    assert Ticket.objects.get(external_id="T2").checked_in_at is None


def test_retry_after_is_understood_as_an_http_date():
    later = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 25 < _retry_after(httpx.Response(429, headers={"Retry-After": later})) <= 30
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "7"})) == 7
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "soon"})) is None
    assert _retry_after(httpx.Response(429)) is None


# ---------------------------------------------------------------- reads of personal data


def test_rows_with_personal_data_need_the_token_to_read(tmp_path):
    run = ImportRun.objects.create(source_path=str(tmp_path / "x.csv"), organizer="festival-a")
    client = Client()
    for path in (
        f"/api/v1/imports/runs/{run.pk}/quarantine",
        "/api/v1/connector/dead-letters",
        "/api/v1/events/E/manifest",
    ):
        assert client.get(path).status_code == 401
        assert operator().get(path).status_code == 200


# ---------------------------------------------------------------- operations


def test_probes_answer_with_a_pod_ip_as_the_host():
    for path in ("/livez", "/healthz", "/metrics"):
        assert Client().get(path, headers={"Host": "10.42.0.17:8000"}).status_code == 200
    assert Client().get("/", headers={"Host": "10.42.0.17:8000"}).status_code == 400


def test_kubernetes_edge_config_differs_from_compose_only_in_addresses():
    root = Path(__file__).parent.parent / "deploy"
    compose = (root / "nginx.conf").read_text(encoding="utf-8").splitlines()
    cluster = (root / "k8s" / "nginx.conf").read_text(encoding="utf-8").splitlines()
    assert len(compose) == len(cluster)
    different = [
        left.strip().split()[0] for left, right in zip(compose, cluster, strict=True) if left != right
    ]
    assert different == ["resolver", "server"]


def test_imports_and_ticketing_use_separate_queues(settings):
    assert settings.CELERY_TASK_ROUTES["imports.run_import"]["queue"] == "imports"
    assert "ticketing.flush_outbox" not in settings.CELERY_TASK_ROUTES
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert "--queues=imports" in compose
    assert "--queues=celery" in compose


def test_metrics_report_what_the_alerts_watch():
    OutboundCheckin.objects.create(ticket_external_id="T1", idempotency_key="k1")
    body = Client().get("/metrics").content.decode()
    assert "encore_outbox_pending 1\n" in body
    alerts = (Path(__file__).parent.parent / "deploy" / "k8s" / "alerts.yaml").read_text(encoding="utf-8")
    exported = {line.split()[0] for line in body.splitlines() if line.startswith("encore_")}
    named = {
        word for word in alerts.replace("(", " ").replace(")", " ").split() if word.startswith("encore_")
    }
    assert named, "the alert file names no metric"
    assert named <= exported, named - exported


def test_slow_requests_are_logged_as_fields_and_every_reply_has_a_request_id(settings, caplog):
    settings.ENCORE_SLOW_REQUEST_MS = 0
    with caplog.at_level(logging.INFO, logger="encore.request"):
        reply = Client().get("/livez", headers={"X-Request-ID": "abc-123"})
    assert reply.headers["X-Request-ID"] == "abc-123"
    record = next(r for r in caplog.records if r.name == "encore.request")
    assert record.fields["request_id"] == "abc-123"
    assert record.fields["path"] == "/livez"
    assert Client().get("/livez").headers["X-Request-ID"]


def test_a_killed_run_resumes_itself_when_auto_resume_is_on(settings, tmp_path, monkeypatch):
    settings.ENCORE_IMPORT_AUTO_RESUME = True
    settings.ENCORE_IMPORT_STALE_SECONDS = 0
    sent: list[int] = []
    monkeypatch.setattr("imports.tasks.run_import.delay", sent.append)
    path = csv_file(tmp_path, "1,a@example.test,,,,,,\n")
    run = ImportRun.objects.create(source_path=str(path), organizer="festival-a")
    _claim(run.pk)
    ImportRun.objects.filter(pk=run.pk).update(heartbeat_at=timezone.now() - dt.timedelta(seconds=5))
    assert reap_stale() == [run.pk]
    assert sent == [run.pk]

    ImportRun.objects.filter(pk=run.pk).update(
        status=ImportRun.Status.RUNNING,
        resume_count=settings.ENCORE_IMPORT_MAX_AUTO_RESUMES,
        heartbeat_at=timezone.now() - dt.timedelta(seconds=5),
    )
    sent.clear()
    reap_stale()
    assert sent == [], "A run that keeps crashing must wait for a person"


# ---------------------------------------------------------------- query counts


def test_query_count_does_not_grow_with_batch_size(tmp_path):
    small = "".join(f"{i},p{i}@example.test,,,,,,\n" for i in range(1, 11))
    big = "".join(f"{i},q{i}@example.test,,,,,,\n" for i in range(100, 200))
    counts = []
    for name, body in (("small.csv", small), ("big.csv", big)):
        run = ImportRun.objects.create(
            source_path=str(csv_file(tmp_path, body, name)), organizer="festival-a"
        )
        records = [
            Record(index + 1, dict(zip(HEADER.strip().split(","), line.split(","), strict=True)), 0)
            for index, line in enumerate(body.splitlines())
        ]
        with CaptureQueriesContext(connection) as queries, transaction.atomic():
            process_batch(run, records, None)
        counts.append(len(queries))
    assert counts[0] == counts[1], counts


def test_run_list_query_count_does_not_grow_with_runs():
    client = Client()
    for index in range(3):
        ImportRun.objects.create(source_path=f"/x{index}.csv", organizer=f"o{index}")
    with CaptureQueriesContext(connection) as few:
        client.get("/api/v1/imports/runs")
    for index in range(20):
        ImportRun.objects.create(source_path=f"/y{index}.csv", organizer=f"p{index}")
    with CaptureQueriesContext(connection) as many:
        client.get("/api/v1/imports/runs")
    assert len(few) == len(many)


# ---------------------------------------------------------------- the commands an operator runs


def test_operator_commands_do_what_the_runbooks_say(tmp_path, capsys):
    path = csv_file(tmp_path, "1,anna@example.test,,Anna,,,,\n")
    call_command("run_import", str(path), "--organizer", "festival-a", "--dry-run")
    call_command("run_import", str(path), "--organizer", "festival-a")
    capsys.readouterr()

    call_command("cutover_report", "festival-a")
    assert json.loads(capsys.readouterr().out)["go"] is True
    with pytest.raises(SystemExit):
        call_command("cutover_report", "nobody")
    capsys.readouterr()

    call_command("erase_subject", "festival-a", "anna@example.test")
    assert json.loads(capsys.readouterr().out)["contacts_removed"] == 1

    ledger = tmp_path / "ledger.json"
    call_command("replay_erasures", "--dump", str(ledger))
    ErasedSubject.objects.all().delete()
    Contact.objects.create(organizer="festival-a", email="anna@example.test")
    capsys.readouterr()
    call_command("replay_erasures", "--ledger", str(ledger))
    assert json.loads(capsys.readouterr().out)["contacts_removed"] == 1
    assert not Contact.objects.filter(email="anna@example.test").exists()


# ---------------------------------------------------------------- go or no-go


def test_cutover_report_says_no_until_every_check_passes(tmp_path):
    assert cutover_report("festival-a")["go"] is False
    path = csv_file(tmp_path, "1,a@example.test,,,,,,\n2,b@example.test,,,,,,\n")
    run_for(path, "festival-a")
    no_dry = cutover_report("festival-a")
    assert no_dry["go"] is False
    assert [c["name"] for c in no_dry["checks"] if not c["passed"]] == ["A dry run came first"]

    run_for(path, "club-b", dry_run=True)
    run_for(path, "club-b")
    report = cutover_report("club-b")
    assert report["go"] is True, report
    assert report["contacts"] == 2

    bad = csv_file(tmp_path, "3,c@example.test,,,,,,\n4,not-an-email,,,,,,\n", "bad.csv")
    run_for(bad, "club-b", dry_run=True)
    run_for(bad, "club-b")
    report = cutover_report("club-b")
    assert report["go"] is False
    assert [c["name"] for c in report["checks"] if not c["passed"]] == [
        "Open quarantine at most 0.5% of rows"
    ]
