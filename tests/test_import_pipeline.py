import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from django.db import IntegrityError, transaction

from crm.models import Contact, ContactAlias
from imports.management.commands.gen_legacy import generate
from imports.mapping import RowDefect
from imports.models import ImportRun, QuarantineRow
from imports.pipeline import (
    LeaseLost,
    RunBusy,
    _claim,
    _commit_batch,
    execute_run,
    reap_stale_runs,
    reprocess_quarantined,
    rollback_run,
)
from imports.reconcile import reconcile_run
from imports.source import Record, iter_csv, read_header

pytestmark = pytest.mark.django_db(transaction=True)

HEADER = "CUST_ID,EMAIL_ADDR,MOBILE,FNAME,LNAME,TOWN,OPT_IN,CREATED\n"


def write(tmp_path: Path, body: str, name: str = "src.csv") -> Path:
    path = tmp_path / name
    path.write_bytes((HEADER + body).encode("utf-8", "surrogateescape"))
    return path


def start(path: Path, **kwargs) -> ImportRun:
    return ImportRun.objects.create(source_path=str(path), **kwargs)


def run_file(path: Path, batch_size: int = 2, **kwargs) -> ImportRun:
    return execute_run(start(path, **kwargs).pk, batch_size=batch_size)


def accounted(run: ImportRun) -> int:
    return run.rows_created + run.rows_updated + run.rows_merged + run.rows_skipped + run.rows_quarantined


def test_counts_account_for_every_row_and_reconcile(tmp_path):
    path = write(
        tmp_path,
        "1,a@x.io,+391234567,A,B,Rome,Y,2020-01-02\n"
        "2,not-an-email,,A,B,,,\n"
        "3,A@X.IO,,Second,,,,\n"
        ",c@x.io,,No,Id,,,\n"
        "5,e@x.io,,E,,,N,31/12/2019\n",
    )
    run = run_file(path)
    assert run.status == "completed"
    assert (run.rows_read, run.rows_created, run.rows_merged, run.rows_quarantined) == (5, 2, 1, 2)
    assert accounted(run) == run.rows_read
    assert run.reconciliation["balanced"] is True
    assert set(QuarantineRow.objects.values_list("reason", flat=True)) == {"invalid_email", "missing_id"}


def test_second_run_of_same_file_creates_nothing(tmp_path):
    path = write(tmp_path, "1,a@x.io,,A,,,,\n2,b@x.io,,B,,,,\n3,a@x.io,,C,,,,\n")
    run_file(path)
    contacts, aliases = Contact.objects.count(), ContactAlias.objects.count()
    second = run_file(path)
    assert (second.rows_created, second.rows_updated, second.rows_merged) == (0, 0, 0)
    assert second.rows_skipped == 3
    assert (Contact.objects.count(), ContactAlias.objects.count()) == (contacts, aliases)


def test_unknown_stays_null_not_false_or_blank(tmp_path):
    run_file(write(tmp_path, "1,a@x.io,,,,,,\n"))
    contact = Contact.objects.get()
    assert contact.marketing_opt_in is None
    assert contact.city is None
    assert contact.legacy_created_at is None


def test_import_does_not_overwrite_curated_field_or_erase_with_blank(tmp_path):
    run_file(write(tmp_path, "1,a@x.io,+391234567,Anna,,Rome,,\n", "one.csv"))
    Contact.objects.update(curated_fields=["city"], city="Hand edited")
    run_file(write(tmp_path, "1,a@x.io,,Anne,,Milan,,\n", "two.csv"))
    contact = Contact.objects.get()
    assert contact.first_name == "Anne"
    assert contact.city == "Hand edited"
    assert contact.phone == "+391234567"


def test_merge_only_fills_empty_values(tmp_path):
    run_file(write(tmp_path, "1,a@x.io,,Anna,,,,\n2,a@x.io,+391234567,Other,Rossi,,,\n"))
    contact = Contact.objects.get()
    assert (contact.first_name, contact.last_name, contact.phone) == ("Anna", "Rossi", "+391234567")
    assert contact.aliases.count() == 2


def test_update_to_email_owned_by_someone_else_goes_to_quarantine(tmp_path):
    run_file(write(tmp_path, "1,a@x.io,,A,,,,\n2,b@x.io,,B,,,,\n", "one.csv"))
    run = run_file(write(tmp_path, "2,a@x.io,,B,,,,\n", "two.csv"))
    assert run.rows_quarantined == 1
    assert QuarantineRow.objects.get(run=run).reason == "email_conflict"
    assert Contact.objects.filter(email="b@x.io").exists()


def test_crash_between_batches_resumes_without_duplicates(tmp_path):
    path = tmp_path / "gen.csv"
    generate(path, rows=500)
    run = start(path)

    class Crash(Exception):
        pass

    def crash_on_third(committed: int) -> None:
        if committed == 3:
            raise Crash

    with pytest.raises(Crash):
        execute_run(run.pk, batch_size=50, after_batch=crash_on_third)
    run.refresh_from_db()
    assert run.status == "failed"
    assert run.checkpoint_row == 150

    finished = execute_run(run.pk, batch_size=50)
    assert finished.status == "completed"
    assert finished.resume_count == 1
    assert finished.rows_read == 500
    assert accounted(finished) == 500
    assert finished.reconciliation["balanced"] is True
    assert finished.reconciliation["discrepancies"] == 0


def test_failure_inside_a_batch_commits_none_of_that_batch(tmp_path, monkeypatch):
    path = write(tmp_path, "".join(f"{i},u{i}@x.io,,,,,,\n" for i in range(1, 7)))
    run = start(path)
    import imports.pipeline as pipeline

    real = pipeline.process_batch
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        result = real(*args, **kwargs)
        if calls["n"] == 2:
            raise RuntimeError("database went away after the rows were written")
        return result

    monkeypatch.setattr(pipeline, "process_batch", flaky)
    with pytest.raises(RuntimeError):
        execute_run(run.pk, batch_size=2)
    run.refresh_from_db()
    assert Contact.objects.count() == 2
    assert run.rows_read == 2
    assert run.checkpoint_row == 2

    monkeypatch.setattr(pipeline, "process_batch", real)
    finished = execute_run(run.pk, batch_size=2)
    assert Contact.objects.count() == 6
    assert finished.reconciliation["balanced"] is True


def test_killing_a_real_process_then_resuming_loses_and_duplicates_nothing(tmp_path, django_db_setup):
    from django.conf import settings

    path = tmp_path / "kill.csv"
    generate(path, rows=60_000)
    run = start(path)
    db = settings.DATABASES["default"]
    env = {
        **os.environ,
        "ENCORE_DEBUG": "1",
        "DATABASE_URL": f"postgres://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}",
    }
    process = subprocess.Popen(
        [sys.executable, "manage.py", "run_import", "--resume", str(run.pk), "--batch-size", "500"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        run.refresh_from_db()
        if run.batches_committed >= 3:
            break
        if process.poll() is not None:
            pytest.fail(f"Importer ended early: {process.stderr.read().decode()[-1500:]}")
        time.sleep(0.02)
    process.kill()
    process.wait()
    run.refresh_from_db()
    assert run.status == "running", "The kill must land before the importer ends"
    assert 0 < run.rows_read < 60_000

    assert reap_stale_runs(0) == [run.pk]
    finished = execute_run(run.pk, batch_size=500)
    assert finished.rows_read == 60_000
    assert finished.reconciliation["discrepancies"] == 0
    assert ContactAlias.objects.count() == finished.rows_created + finished.rows_merged


def test_stale_worker_loses_its_lease_and_cannot_write(tmp_path):
    path = write(tmp_path, "1,a@x.io,,,,,,\n2,b@x.io,,,,,,\n")
    run = start(path)
    claimed, old_token = _claim(run.pk)
    reap_stale_runs(0)
    _, new_token = _claim(run.pk)
    assert new_token != old_token
    record = Record(1, {"CUST_ID": "1", "EMAIL_ADDR": "a@x.io"}, 10)
    with pytest.raises(LeaseLost):
        _commit_batch(claimed, old_token, [record], None)
    assert Contact.objects.count() == 0


def test_only_one_write_import_runs_at_a_time(tmp_path):
    path = write(tmp_path, "1,a@x.io,,,,,,\n")
    first, second = start(path), start(path)
    _claim(first.pk)
    with pytest.raises(RunBusy):
        _claim(second.pk)
    assert ImportRun.objects.get(pk=second.pk).status == "pending"


def test_counter_constraint_rejects_rows_that_vanish(tmp_path):
    run = start(tmp_path / "x.csv")
    with pytest.raises(IntegrityError), transaction.atomic():
        ImportRun.objects.filter(pk=run.pk).update(rows_read=5)


def test_resume_refuses_a_changed_source_file(tmp_path):
    path = write(tmp_path, "1,a@x.io,,,,,,\n2,b@x.io,,,,,,\n")
    run = start(path)

    class Stop(Exception):
        pass

    def stop(_committed: int) -> None:
        raise Stop

    with pytest.raises(Stop):
        execute_run(run.pk, batch_size=1, after_batch=stop)
    path.write_text(HEADER + "9,z@x.io,,,,,,\n", encoding="utf-8")
    result = execute_run(run.pk, batch_size=1)
    assert result.status == "failed"
    assert "changed" in result.error


def test_dry_run_writes_no_contacts_and_predicts_the_real_run(tmp_path):
    path = tmp_path / "gen.csv"
    generate(path, rows=400)
    dry = execute_run(start(path, dry_run=True).pk, batch_size=30)
    assert Contact.objects.count() == 0
    assert ContactAlias.objects.count() == 0
    real = execute_run(start(path).pk, batch_size=30)
    predicted = (dry.rows_created, dry.rows_merged, dry.rows_quarantined)
    assert predicted == (real.rows_created, real.rows_merged, real.rows_quarantined)


def test_rollback_restores_the_exact_prior_state_and_allows_reimport(tmp_path):
    run_file(write(tmp_path, "1,a@x.io,,Anna,,Rome,,\n", "base.csv"))
    second = run_file(write(tmp_path, "1,a@x.io,,Anne,,Milan,,\n2,b@x.io,,B,,,,\n", "next.csv"))
    assert Contact.objects.count() == 2
    rollback_run(second.pk)
    contact = Contact.objects.get()
    assert (contact.first_name, contact.city) == ("Anna", "Rome")
    assert not ContactAlias.objects.filter(src_uid="legacy:2").exists()
    again = run_file(write(tmp_path, "1,a@x.io,,Anne,,Milan,,\n2,b@x.io,,B,,,,\n", "next2.csv"))
    assert (again.rows_updated, again.rows_created) == (1, 1)


def test_rollback_refuses_when_a_later_run_exists(tmp_path):
    first = run_file(write(tmp_path, "1,a@x.io,,,,,,\n", "a.csv"))
    run_file(write(tmp_path, "2,b@x.io,,,,,,\n", "b.csv"))
    with pytest.raises(RunBusy):
        rollback_run(first.pk)


def test_quarantined_row_can_be_corrected_and_counters_stay_exact(tmp_path):
    run = run_file(write(tmp_path, "1,bad-email,,A,,,,\n"))
    row = QuarantineRow.objects.get(run=run)
    with pytest.raises(RowDefect):
        reprocess_quarantined(row, {"EMAIL_ADDR": "still bad"})
    reprocess_quarantined(row, {"EMAIL_ADDR": "good@x.io"})
    run.refresh_from_db()
    assert (run.rows_quarantined, run.rows_created) == (0, 1)
    assert accounted(run) == run.rows_read == 1
    assert Contact.objects.filter(email="good@x.io").exists()
    assert reconcile_run(run)["balanced"] is True


def test_reconciliation_detects_tampering_after_import(tmp_path):
    run = run_file(write(tmp_path, "1,a@x.io,,,,,,\n2,b@x.io,,,,,,\n3,c@x.io,,,,,,\n"))
    ContactAlias.objects.get(src_uid="legacy:1").delete()
    Contact.objects.filter(email="b@x.io").update(email="changed@x.io")
    report = reconcile_run(run)
    assert report["balanced"] is False
    assert report["missing_in_target"] == 1
    assert report["field_mismatches"] == 1


def test_same_id_twice_in_file_keeps_the_last_version_and_reconciles(tmp_path):
    run = run_file(write(tmp_path, "1,a@x.io,,First,,,,\n1,a@x.io,,Last,,,,\n"), batch_size=1)
    assert Contact.objects.get().first_name == "Last"
    assert run.reconciliation["balanced"] is True
    same_batch = run_file(
        write(tmp_path, "2,b@x.io,,First,,,,\n2,b@x.io,,Last,,,,\n", "b.csv"), batch_size=50
    )
    assert Contact.objects.get(email="b@x.io").first_name == "Last"
    assert same_batch.reconciliation["balanced"] is True


def test_damaged_bytes_and_wrong_column_count_go_to_quarantine(tmp_path):
    path = write(tmp_path, "1,a\udce9@x.io,,,,,,\n2,b@x.io\n3,c@x.io,,,,,,\n")
    run = run_file(path)
    assert run.status == "completed"
    assert set(QuarantineRow.objects.values_list("reason", flat=True)) == {"bad_encoding", "column_count"}
    assert run.rows_created == 1


def test_quoted_multiline_offsets_restart_exactly(tmp_path):
    path = write(tmp_path, '1,a@x.io,,,,"Milano,\nCentro",,\n2,b@x.io,,,,,,\n')
    header, offset = read_header(path)
    first = next(iter_csv(path, header, offset, 0))
    assert first.values["TOWN"] == "Milano,\nCentro"
    second = next(iter_csv(path, header, first.end_offset, first.row_number))
    assert (second.row_number, second.values["CUST_ID"]) == (2, "2")


def test_unmapped_columns_are_reported_and_missing_required_column_stops_run(tmp_path):
    path = tmp_path / "gen.csv"
    generate(path, rows=5)
    assert execute_run(start(path).pk).unmapped_columns == ["LOYALTY_TIER_OLD"]
    bad = tmp_path / "bad.csv"
    bad.write_text("EMAIL_ADDR\na@x.io\n", encoding="utf-8")
    run = execute_run(start(bad).pk)
    assert run.status == "failed"
    assert "CUST_ID" in run.error


def test_jsonl_source(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        '{"CUST_ID": 1, "EMAIL_ADDR": "a@x.io"}\nnot json\n{"CUST_ID": 2, "MOBILE": null}\n', "utf-8"
    )
    run = execute_run(start(path, source_format="jsonl").pk)
    assert (run.rows_read, run.rows_created, run.rows_quarantined) == (3, 1, 2)
    assert run.reconciliation["balanced"] is True


def test_rollback_restores_alias_hashes_changed_in_a_later_batch(tmp_path):
    run_file(write(tmp_path, "1,a@x.io,,One,,,,\n2,a@x.io,,,Two,,,\n", "base.csv"))
    # The same contact is touched through two aliases, in two different batches of one run.
    body = "1,a@x.io,,Changed,,,,\n2,a@x.io,,,Changed,,,\n"
    second = run_file(write(tmp_path, body, "next.csv"), batch_size=1)
    rollback_run(second.pk)
    again = run_file(write(tmp_path, body, "next2.csv"), batch_size=1)
    assert (again.rows_updated, again.rows_skipped) == (2, 0)


@pytest.mark.parametrize("state", ["dry", "rolled_back", "running"])
def test_quarantine_retry_cannot_write_into_a_run_that_is_not_live(tmp_path, state):
    run = run_file(write(tmp_path, "1,bad-email,,A,,,,\n"), dry_run=state == "dry")
    if state == "rolled_back":
        rollback_run(run.pk)
    if state == "running":
        ImportRun.objects.filter(pk=run.pk).update(status="running")
    with pytest.raises(RunBusy):
        reprocess_quarantined(QuarantineRow.objects.get(run=run), {"EMAIL_ADDR": "ok@x.io"})
    assert Contact.objects.count() == 0


def test_run_detail_reports_real_batches_and_speed(tmp_path):
    from rest_framework.test import APIClient

    run = run_file(write(tmp_path, "".join(f"{i},u{i}@x.io,,,,,,\n" for i in range(1, 7))), batch_size=2)
    body = APIClient().get(f"/api/v1/imports/runs/{run.pk}").json()
    assert [batch["number"] for batch in body["batches"]] == [3, 2, 1]
    assert sum(batch["rows"] for batch in body["batches"]) == 6
    assert "lease_token" not in body


def test_batch_log_never_overstates_after_a_failed_batch(tmp_path, monkeypatch):
    import imports.pipeline as pipeline
    from imports.models import ImportBatch

    run = start(write(tmp_path, "".join(f"{i},u{i}@x.io,,,,,,\n" for i in range(1, 5))))
    calls = {"n": 0}
    original = pipeline.process_batch

    def fail_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "process_batch", fail_second)
    with pytest.raises(RuntimeError):
        execute_run(run.pk, batch_size=2)
    assert ImportBatch.objects.filter(run=run).count() == 1


def test_quarantine_pages_report_the_real_total(tmp_path, settings):
    from rest_framework.test import APIClient

    settings.ENCORE_OPERATOR_TOKEN = "op"

    run = run_file(write(tmp_path, "".join(f"{i},bad,,,,,,\n" for i in range(1, 251))), batch_size=100)
    api = APIClient(HTTP_AUTHORIZATION="Bearer op")
    first = api.get(f"/api/v1/imports/runs/{run.pk}/quarantine")
    assert first["X-Total-Count"] == "250"
    assert len(first.json()) == 200
    second = api.get(f"/api/v1/imports/runs/{run.pk}/quarantine?offset=200")
    assert len(second.json()) == 50


def test_repeat_reconciliation_runs_in_the_background_and_is_saved(tmp_path, settings, monkeypatch):
    from rest_framework.test import APIClient

    import imports.views as views
    from imports.tasks import reconcile_task

    settings.ENCORE_OPERATOR_TOKEN = "op"
    queued: list[int] = []
    monkeypatch.setattr(views.reconcile_task, "delay", queued.append)
    run = run_file(write(tmp_path, "1,a@x.io,,,,,,\n2,b@x.io,,,,,,\n"))
    ContactAlias.objects.get(src_uid="legacy:1").delete()
    api = APIClient()
    url = f"/api/v1/imports/runs/{run.pk}/reconcile"

    reply = api.post(url, HTTP_AUTHORIZATION="Bearer op")
    assert reply.status_code == 202
    assert reply.json()["reconciling"] is True
    assert queued == [run.pk]
    assert api.post(url, HTTP_AUTHORIZATION="Bearer op").status_code == 409, "Only one at a time"

    reconcile_task(run.pk)
    run.refresh_from_db()
    assert run.reconciliation["balanced"] is False
    assert run.reconcile_started_at is None


def test_reconciliation_flag_clears_when_the_task_crashes(tmp_path, monkeypatch):
    from django.utils import timezone

    import imports.tasks as tasks

    run = run_file(write(tmp_path, "1,a@x.io,,,,,,\n"))
    ImportRun.objects.filter(pk=run.pk).update(reconcile_started_at=timezone.now())

    def boom(_run):
        raise RuntimeError("file gone")

    monkeypatch.setattr(tasks, "reconcile_run", boom)
    with pytest.raises(RuntimeError):
        tasks.reconcile_task(run.pk)
    run.refresh_from_db()
    assert run.reconcile_started_at is None
    assert run.reconciliation["balanced"] is True, "The last good report stays"
    assert "file gone" in run.reconcile_error


def test_slow_reconciliation_is_not_mistaken_for_a_dead_worker(tmp_path, monkeypatch, settings):
    """Found by the 1,000,000 row measurement: the reaper interrupted a healthy run during reconciliation."""
    import imports.pipeline as pipeline

    settings.ENCORE_IMPORT_HEARTBEAT_SECONDS = 0.1
    real = pipeline.reconcile_run
    reaped: list[list[int]] = []

    def slow(run):
        time.sleep(1.0)
        reaped.append(reap_stale_runs(0.5))
        return real(run)

    monkeypatch.setattr(pipeline, "reconcile_run", slow)
    run = run_file(write(tmp_path, "1,a@x.io,,,,,,\n"))
    assert reaped == [[]]
    assert run.status == "completed"
    assert run.reconciliation["balanced"] is True


def test_run_that_lost_its_lease_while_finishing_does_not_claim_success(tmp_path, monkeypatch):
    import imports.pipeline as pipeline

    real = pipeline.reconcile_run

    def taken_over(run):
        ImportRun.objects.filter(pk=run.pk).update(status="interrupted", lease_token="")
        return real(run)

    monkeypatch.setattr(pipeline, "reconcile_run", taken_over)
    run = start(write(tmp_path, "1,a@x.io,,,,,,\n"))
    with pytest.raises(LeaseLost):
        execute_run(run.pk)
    assert ImportRun.objects.get(pk=run.pk).status == "interrupted"


def test_heartbeat_stops_when_the_run_ends(tmp_path, settings):
    settings.ENCORE_IMPORT_HEARTBEAT_SECONDS = 0.05
    run = run_file(write(tmp_path, "1,a@x.io,,,,,,\n"))
    first = ImportRun.objects.get(pk=run.pk).heartbeat_at
    time.sleep(0.3)
    assert ImportRun.objects.get(pk=run.pk).heartbeat_at == first
