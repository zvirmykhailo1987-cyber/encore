"""The import run: one transaction per batch, with rows, counters and checkpoint committed together.

A process can die at any instruction. Either a batch and its checkpoint are both durable, or neither is,
so a resume starts exactly after the last committed batch. A lease token fences off a stale worker that
wakes up after its run was taken over.
"""

import datetime as dt
import json
import logging
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import cast

from django.conf import settings
from django.db import IntegrityError, connection, connections, transaction
from django.db.models import F
from django.utils import timezone

from crm.models import IMPORTABLE_FIELDS, Contact, ContactAlias

from .mapping import COLUMN_MAP, REQUIRED_COLUMNS, CleanRow, RowDefect, clean_row
from .models import AliasChange, ContactChange, ImportBatch, ImportRun, QuarantineRow
from .reconcile import reconcile_run
from .source import Record, SourceError, clean_for_storage, file_sha256, iter_csv, iter_jsonl, read_header

logger = logging.getLogger(__name__)


class RunBusy(Exception):
    """Another write import is running, or this run cannot be started in its current state."""


class LeaseLost(Exception):
    """This worker no longer owns the run. Its batch is discarded."""


@dataclass
class BatchResult:
    counts: Counter = field(default_factory=Counter)
    quarantine: list[QuarantineRow] = field(default_factory=list)


@dataclass
class DryRunOverlay:
    """What a dry run would have written, so later batches classify as a real run would."""

    aliases: dict[str, str] = field(default_factory=dict)
    emails: set[str] = field(default_factory=set)


def contact_snapshot(contact: Contact) -> dict:
    return json.loads(json.dumps({name: getattr(contact, name) for name in IMPORTABLE_FIELDS}, default=str))


def _restore_value(name: str, value):
    if name == "legacy_created_at" and value is not None:
        return dt.date.fromisoformat(value)
    return value


def restore_snapshot(contact: Contact, snapshot: dict) -> None:
    for name, value in snapshot.items():
        setattr(contact, name, _restore_value(name, value))


def records_for(run: ImportRun, path: Path) -> Iterator[Record]:
    if run.source_format == "jsonl":
        return iter_jsonl(path, run.checkpoint_offset, run.checkpoint_row)
    return iter_csv(path, run.header, run.checkpoint_offset, run.checkpoint_row)


def _defect_row(run: ImportRun, record: Record, reason: str, detail: str = "") -> QuarantineRow:
    raw = record.raw
    if raw is None and record.values is not None:
        raw = {key: clean_for_storage(value) for key, value in record.values.items()}
    return QuarantineRow(
        run=run, row_number=record.row_number, reason=reason, detail=clean_for_storage(detail), raw=raw or {}
    )


def copy_contacts(contacts: list[Contact]) -> None:
    """Insert through COPY, in the batch's transaction. Ids are reserved first because aliases need them."""
    if not contacts:
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT nextval(pg_get_serial_sequence('crm_contact', 'id')) FROM generate_series(1, %s)",
            [len(contacts)],
        )
        for contact, (pk,) in zip(contacts, cursor.fetchall(), strict=True):
            contact.pk = pk
    now = timezone.now()
    columns = ("id", *IMPORTABLE_FIELDS, "curated_fields", "created_by_run_id", "created_at", "updated_at")
    with connection.connection.cursor() as raw:
        with raw.copy(f"COPY crm_contact ({', '.join(columns)}) FROM STDIN") as copy:  # noqa: S608
            for contact in contacts:
                values = [getattr(contact, name) for name in IMPORTABLE_FIELDS]
                copy.write_row((contact.pk, *values, "[]", contact.created_by_run_id, now, now))


def copy_aliases(aliases: list[ContactAlias]) -> None:
    if not aliases:
        return
    with connection.connection.cursor() as raw:
        with raw.copy("COPY crm_contactalias (src_uid, contact_id, row_hash, run_id) FROM STDIN") as copy:
            for alias in aliases:
                copy.write_row((alias.src_uid, alias.contact.pk, alias.row_hash, alias.run_id))


def _apply_clean_rows(
    run: ImportRun,
    rows: list[tuple[CleanRow, Record]],
    result: BatchResult,
    overlay: DryRunOverlay | None,
) -> None:
    src_uids = [row.src_uid for row, _ in rows]
    emails = {str(row.fields["email"]) for row, _ in rows if row.fields["email"]}
    aliases = {
        a.src_uid: a for a in ContactAlias.objects.select_related("contact").filter(src_uid__in=src_uids)
    }
    contacts_by_email = {c.email: c for c in Contact.objects.filter(email__in=emails)}
    for known in aliases.values():
        if known.contact.email:
            contacts_by_email.setdefault(known.contact.email, known.contact)

    new_contacts: list[Contact] = []
    new_aliases: list[ContactAlias] = []
    changed: dict[int, Contact] = {}
    snapshots: dict[int, ContactChange] = {}
    updated_aliases: list[ContactAlias] = []
    alias_before: dict[str, str] = {}

    def remember(contact: Contact) -> None:
        if contact.pk is None or contact.pk in snapshots:
            return
        snapshots[contact.pk] = ContactChange(run=run, contact=contact, before=contact_snapshot(contact))
        changed[contact.pk] = contact

    for row, record in rows:
        alias = aliases.get(row.src_uid)
        known_hash = overlay.aliases.get(row.src_uid) if overlay else None
        if overlay is not None and alias is None and known_hash is not None:
            result.counts["skipped" if known_hash == row.row_hash else "updated"] += 1
            overlay.aliases[row.src_uid] = row.row_hash
            continue
        if alias is not None:
            if alias.row_hash == row.row_hash:
                result.counts["skipped"] += 1
                continue
            contact = alias.contact
            new_email = cast(str | None, row.fields["email"])
            owner = contacts_by_email.get(new_email) if new_email else None
            if new_email and new_email != contact.email and owner is not None and owner is not contact:
                result.quarantine.append(_defect_row(run, record, "email_conflict", str(new_email)))
                result.counts["quarantined"] += 1
                continue
            remember(contact)
            for name, value in row.fields.items():
                if value is not None and name not in contact.curated_fields:
                    setattr(contact, name, value)
            if contact.email:
                contacts_by_email[contact.email] = contact
            if alias.pk is not None:
                alias_before.setdefault(alias.src_uid, alias.row_hash)
                updated_aliases.append(alias)
            alias.row_hash = row.row_hash
            result.counts["updated"] += 1
            continue

        email = cast(str | None, row.fields["email"])
        existing = contacts_by_email.get(email) if email else None
        if existing is None and overlay and email in overlay.emails:
            result.counts["merged"] += 1
            overlay.aliases[row.src_uid] = row.row_hash
            continue
        if existing is not None:
            remember(existing)
            for name, value in row.fields.items():
                if value is not None and getattr(existing, name) is None:
                    setattr(existing, name, value)
            alias = ContactAlias(src_uid=row.src_uid, contact=existing, row_hash=row.row_hash, run=run)
            result.counts["merged"] += 1
        else:
            contact = Contact(created_by_run=run, **row.fields)  # type: ignore[arg-type]
            new_contacts.append(contact)
            if email:
                contacts_by_email[email] = contact
            alias = ContactAlias(src_uid=row.src_uid, contact=contact, row_hash=row.row_hash, run=run)
            result.counts["created"] += 1
        aliases[row.src_uid] = alias
        new_aliases.append(alias)

    if overlay is not None:
        for alias in new_aliases:
            overlay.aliases[alias.src_uid] = alias.row_hash
            if alias.contact.email:
                overlay.emails.add(alias.contact.email)
        return

    copy_contacts(new_contacts)
    copy_aliases(new_aliases)
    if snapshots:
        ContactChange.objects.bulk_create(list(snapshots.values()), batch_size=2000, ignore_conflicts=True)
        now = timezone.now()
        for contact in changed.values():
            contact.updated_at = now
        Contact.objects.bulk_update(
            list(changed.values()), [*IMPORTABLE_FIELDS, "updated_at"], batch_size=2000
        )
    if updated_aliases:
        AliasChange.objects.bulk_create(
            [AliasChange(run=run, src_uid=uid, before_hash=value) for uid, value in alias_before.items()],
            batch_size=2000,
            ignore_conflicts=True,
        )
        ContactAlias.objects.bulk_update(updated_aliases, ["row_hash"], batch_size=2000)


def process_batch(run: ImportRun, records: Iterable[Record], overlay: DryRunOverlay | None) -> BatchResult:
    result = BatchResult()
    clean: list[tuple[CleanRow, Record]] = []
    for record in records:
        result.counts["read"] += 1
        if record.defect or record.values is None:
            result.quarantine.append(_defect_row(run, record, record.defect or "unreadable"))
            result.counts["quarantined"] += 1
            continue
        try:
            clean.append((clean_row(record.row_number, record.values, run.source_system), record))
        except RowDefect as defect:
            result.quarantine.append(_defect_row(run, record, defect.reason, defect.detail))
            result.counts["quarantined"] += 1
    _apply_clean_rows(run, clean, result, overlay)
    return result


class Heartbeat(threading.Thread):
    """Proves the process is alive during long work between commits: hashing a big file, reconciliation.

    It writes only while this worker holds the lease. A killed process stops beating, which is the signal
    the reaper needs. A stuck process keeps beating; that is a separate failure this does not detect.
    """

    def __init__(self, run_id: int, token: str, interval: float) -> None:
        super().__init__(daemon=True)
        self.run_id = run_id
        self.token = token
        self.interval = interval
        self.finished = threading.Event()

    def run(self) -> None:
        try:
            while not self.finished.wait(self.interval):
                ImportRun.objects.filter(pk=self.run_id, lease_token=self.token).update(
                    heartbeat_at=timezone.now()
                )
        finally:
            connections.close_all()

    def stop(self) -> None:
        self.finished.set()
        self.join()


def _claim(run_id: int) -> tuple[ImportRun, str]:
    token = str(uuid.uuid4())
    try:
        with transaction.atomic():
            run = ImportRun.objects.select_for_update().get(pk=run_id)
            if run.status not in ImportRun.RESUMABLE:
                raise RunBusy(f"Run {run_id} is {run.status}")
            run.resume_count += 1 if run.status != ImportRun.Status.PENDING else 0
            run.status = ImportRun.Status.RUNNING
            run.lease_token = token
            run.heartbeat_at = timezone.now()
            run.started_at = run.started_at or run.heartbeat_at
            run.pause_requested = False
            run.error = ""
            run.save()
    except IntegrityError as exc:
        raise RunBusy("Another write import is already running") from exc
    return run, token


def _prepare(run: ImportRun, path: Path) -> None:
    digest = file_sha256(path)
    if run.source_sha256 and run.source_sha256 != digest:
        raise SourceError("The source file changed since this run started. Start a new run for the new file.")
    run.source_sha256 = digest
    run.source_size = path.stat().st_size
    if run.source_format == "csv" and not run.header:
        header, offset = read_header(path)
        missing = [column for column in REQUIRED_COLUMNS if column not in header]
        if missing:
            raise SourceError(f"Required columns are missing: {', '.join(missing)}")
        run.header = header
        run.unmapped_columns = [column for column in header if column not in COLUMN_MAP]
        run.checkpoint_offset = offset
    ImportRun.objects.filter(pk=run.pk).update(
        source_sha256=run.source_sha256,
        source_size=run.source_size,
        header=run.header,
        unmapped_columns=run.unmapped_columns,
        checkpoint_offset=run.checkpoint_offset,
    )


def _commit_batch(run: ImportRun, token: str, batch: list[Record], overlay: DryRunOverlay | None) -> bool:
    """Commit one batch. Returns True when an operator asked this run to pause."""
    started = time.monotonic()
    with transaction.atomic():
        current = ImportRun.objects.select_for_update().get(pk=run.pk)
        if current.lease_token != token or current.status != ImportRun.Status.RUNNING:
            raise LeaseLost(f"Run {run.pk} is no longer owned by this worker")
        result = process_batch(run, batch, overlay)
        QuarantineRow.objects.bulk_create(result.quarantine, batch_size=2000, ignore_conflicts=True)
        last = batch[-1]
        ImportBatch.objects.create(
            run=run,
            number=current.batches_committed + 1,
            rows=result.counts["read"],
            quarantined=result.counts["quarantined"],
            last_row=last.row_number,
            last_offset=last.end_offset,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        ImportRun.objects.filter(pk=run.pk).update(
            rows_read=F("rows_read") + result.counts["read"],
            rows_created=F("rows_created") + result.counts["created"],
            rows_updated=F("rows_updated") + result.counts["updated"],
            rows_merged=F("rows_merged") + result.counts["merged"],
            rows_skipped=F("rows_skipped") + result.counts["skipped"],
            rows_quarantined=F("rows_quarantined") + result.counts["quarantined"],
            checkpoint_offset=last.end_offset,
            checkpoint_row=last.row_number,
            batches_committed=F("batches_committed") + 1,
            heartbeat_at=timezone.now(),
        )
    return current.pause_requested


def execute_run(
    run_id: int,
    batch_size: int | None = None,
    after_batch: Callable[[int], None] | None = None,
) -> ImportRun:
    """Run or resume an import. `after_batch` receives the committed batch count and exists for tests."""
    run, token = _claim(run_id)
    heartbeat = Heartbeat(run_id, token, settings.ENCORE_IMPORT_HEARTBEAT_SECONDS)
    heartbeat.start()
    try:
        return _execute_claimed(run, token, batch_size, after_batch)
    finally:
        heartbeat.stop()


def _execute_claimed(
    run: ImportRun,
    token: str,
    batch_size: int | None,
    after_batch: Callable[[int], None] | None,
) -> ImportRun:
    run_id = run.pk
    size = batch_size or settings.ENCORE_IMPORT_BATCH_SIZE
    overlay = DryRunOverlay() if run.dry_run else None
    if run.dry_run:
        run.checkpoint_row = 0
        run.checkpoint_offset = 0
        run.header = []
        ImportRun.objects.filter(pk=run.pk).update(
            rows_read=0, rows_created=0, rows_updated=0, rows_merged=0, rows_skipped=0, rows_quarantined=0
        )
        QuarantineRow.objects.filter(run=run).delete()
        ImportBatch.objects.filter(run=run).delete()
        ImportRun.objects.filter(pk=run.pk).update(batches_committed=0)
    try:
        path = Path(run.source_path)
        _prepare(run, path)
        records = records_for(run, path)
        committed = 0
        while batch := list(islice(records, size)):
            pause = _commit_batch(run, token, batch, overlay)
            committed += 1
            if after_batch:
                after_batch(committed)
            if pause:
                ImportRun.objects.filter(pk=run.pk, lease_token=token).update(
                    status=ImportRun.Status.PAUSED, lease_token="", pause_requested=False
                )
                return ImportRun.objects.get(pk=run.pk)
        _finish(run, token)
    except LeaseLost:
        logger.warning("Run %s: lease lost, this worker stops", run_id)
        raise
    except Exception as exc:
        ImportRun.objects.filter(pk=run.pk, lease_token=token).update(
            status=ImportRun.Status.FAILED, error=str(exc)[:2000], lease_token=""
        )
        if not isinstance(exc, SourceError):
            raise
    return ImportRun.objects.get(pk=run.pk)


def _finish(run: ImportRun, token: str) -> None:
    fresh = ImportRun.objects.get(pk=run.pk)
    report = None if fresh.dry_run else reconcile_run(fresh)
    updated = ImportRun.objects.filter(pk=run.pk, lease_token=token).update(
        status=ImportRun.Status.COMPLETED, finished_at=timezone.now(), lease_token="", reconciliation=report
    )
    if not updated:
        raise LeaseLost(f"Run {run.pk} was taken over before it could finish")


def reap_stale_runs(stale_seconds: int | None = None) -> list[int]:
    """Mark runs whose worker stopped sending heartbeats as interrupted, so they can be resumed."""
    limit = stale_seconds if stale_seconds is not None else settings.ENCORE_IMPORT_STALE_SECONDS
    cutoff = timezone.now() - dt.timedelta(seconds=limit)
    stale = list(
        ImportRun.objects.filter(status=ImportRun.Status.RUNNING, heartbeat_at__lt=cutoff).values_list(
            "pk", flat=True
        )
    )
    ImportRun.objects.filter(pk__in=stale, status=ImportRun.Status.RUNNING, heartbeat_at__lt=cutoff).update(
        status=ImportRun.Status.INTERRUPTED, lease_token=""
    )
    return stale


def rollback_run(run_id: int) -> ImportRun:
    """Undo one completed run. Only the latest applied run can be undone, or history would be rewritten."""
    with transaction.atomic():
        run = ImportRun.objects.select_for_update().get(pk=run_id)
        if run.status != ImportRun.Status.COMPLETED or run.dry_run:
            raise RunBusy(f"Run {run_id} cannot be rolled back from status {run.status}")
        later = ImportRun.objects.filter(
            pk__gt=run.pk, dry_run=False, status__in=[ImportRun.Status.COMPLETED, ImportRun.Status.RUNNING]
        )
        if later.exists():
            raise RunBusy("A later import was applied. Roll that one back first.")
        ContactAlias.objects.filter(run=run).delete()
        for change in (
            ContactChange.objects.filter(run=run).select_related("contact").iterator(chunk_size=2000)
        ):
            restore_snapshot(change.contact, change.before)
            change.contact.save(update_fields=[*IMPORTABLE_FIELDS, "updated_at"])
        for alias_change in AliasChange.objects.filter(run=run).iterator(chunk_size=2000):
            ContactAlias.objects.filter(src_uid=alias_change.src_uid).update(
                row_hash=alias_change.before_hash
            )
        AliasChange.objects.filter(run=run).delete()
        Contact.objects.filter(created_by_run=run).delete()
        ContactChange.objects.filter(run=run).delete()
        run.status = ImportRun.Status.ROLLED_BACK
        run.finished_at = timezone.now()
        run.save()
    return run


def reprocess_quarantined(row: QuarantineRow, corrected: dict[str, str]) -> QuarantineRow:
    """Re-import one quarantined row with operator corrections, keeping the run's counters exact."""
    with transaction.atomic():
        run = ImportRun.objects.select_for_update().get(pk=row.run_id)
        if run.dry_run or run.status not in (ImportRun.Status.COMPLETED, ImportRun.Status.PAUSED):
            raise RunBusy(f"Rows of a {'dry' if run.dry_run else run.status} run cannot be reprocessed")
        fresh = QuarantineRow.objects.select_for_update().get(pk=row.pk)
        if fresh.status != QuarantineRow.Status.OPEN:
            raise RunBusy("This row was already handled")
        values = {**{k: str(v) for k, v in fresh.raw.items()}, **corrected}
        record = Record(fresh.row_number, values, run.checkpoint_offset)
        result = process_batch(run, [record], None)
        if result.quarantine:
            problem = result.quarantine[0]
            raise RowDefect(problem.reason, problem.detail)
        fresh.status = QuarantineRow.Status.RESOLVED
        fresh.save(update_fields=["status"])
        ImportRun.objects.filter(pk=run.pk).update(
            rows_quarantined=F("rows_quarantined") - 1,
            rows_created=F("rows_created") + result.counts["created"],
            rows_updated=F("rows_updated") + result.counts["updated"],
            rows_merged=F("rows_merged") + result.counts["merged"],
            rows_skipped=F("rows_skipped") + result.counts["skipped"],
        )
    return fresh
