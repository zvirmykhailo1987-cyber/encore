# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

from django.db import models
from django.db.models import F, Q


class ImportRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        INTERRUPTED = "interrupted"
        PAUSED = "paused"
        FAILED = "failed"
        COMPLETED = "completed"
        ROLLED_BACK = "rolled_back"

    RESUMABLE = (Status.PENDING, Status.INTERRUPTED, Status.PAUSED, Status.FAILED)

    organizer = models.CharField(max_length=40, default="demo")
    source_path = models.CharField(max_length=500)
    source_format = models.CharField(max_length=10, default="csv")
    source_sha256 = models.CharField(max_length=64, blank=True)
    source_system = models.CharField(max_length=40, default="legacy")
    source_size = models.BigIntegerField(default=0)
    dry_run = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error = models.TextField(blank=True)

    # Every counter moves in the same transaction as the rows it describes.
    rows_read = models.BigIntegerField(default=0)
    rows_created = models.BigIntegerField(default=0)
    rows_updated = models.BigIntegerField(default=0)
    rows_merged = models.BigIntegerField(default=0)
    rows_skipped = models.BigIntegerField(default=0)
    rows_quarantined = models.BigIntegerField(default=0)
    #: Records of this run later removed by an erasure request. Reconciliation must expect them to be gone.
    contacts_erased = models.BigIntegerField(default=0)
    aliases_erased = models.BigIntegerField(default=0)

    checkpoint_offset = models.BigIntegerField(default=0)
    checkpoint_row = models.BigIntegerField(default=0)
    header = models.JSONField(default=list, blank=True)
    unmapped_columns = models.JSONField(default=list, blank=True)
    batches_committed = models.IntegerField(default=0)
    resume_count = models.IntegerField(default=0)

    lease_token = models.CharField(max_length=36, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    pause_requested = models.BooleanField(default=False)
    reconciliation = models.JSONField(null=True, blank=True)
    #: Set while a repeat reconciliation runs in the background.
    reconcile_started_at = models.DateTimeField(null=True, blank=True)
    reconcile_error = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # One writer per organizer: two live imports could race on that organizer's email uniqueness.
            # Different organizers share nothing, so they can run side by side.
            models.UniqueConstraint(
                fields=["organizer"],
                condition=Q(status="running", dry_run=False),
                name="single_running_write_import_per_organizer",
            ),
            models.CheckConstraint(
                condition=Q(
                    rows_read=F("rows_created")
                    + F("rows_updated")
                    + F("rows_merged")
                    + F("rows_skipped")
                    + F("rows_quarantined")
                ),
                name="import_counters_account_for_every_row",
            ),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"run {self.pk} ({self.status})"


class ImportBatch(models.Model):
    """One committed batch. Written in the batch's own transaction, so it can never overstate progress."""

    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="batches")
    number = models.IntegerField()
    rows = models.IntegerField()
    quarantined = models.IntegerField()
    last_row = models.BigIntegerField()
    last_offset = models.BigIntegerField()
    duration_ms = models.IntegerField()
    committed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "number"], name="batch_number_once_per_run")]
        ordering = ["-number"]

    def __str__(self) -> str:
        return f"run {self.run_id} batch {self.number}"


class QuarantineRow(models.Model):
    class Status(models.TextChoices):
        OPEN = "open"
        RESOLVED = "resolved"
        DISMISSED = "dismissed"

    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="quarantine")
    row_number = models.BigIntegerField()
    reason = models.CharField(max_length=40, db_index=True)
    detail = models.CharField(max_length=300, blank=True)
    raw = models.JSONField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "row_number"], name="quarantine_row_once_per_run")
        ]
        ordering = ["row_number"]

    def __str__(self) -> str:
        return f"run {self.run_id} row {self.row_number}: {self.reason}"


class ContactChange(models.Model):
    """The first state of a contact a run changed. Rollback restores exactly this."""

    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="changes")
    contact = models.ForeignKey("crm.Contact", on_delete=models.CASCADE)
    before = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "contact"], name="one_snapshot_per_run_contact")
        ]

    def __str__(self) -> str:
        return f"run {self.run_id} contact {self.contact_id}"


class AliasChange(models.Model):
    """The first hash a run replaced on a legacy record, so a re-import after rollback sees the old state."""

    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="alias_changes")
    src_uid = models.CharField(max_length=160)
    before_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "src_uid"], name="one_hash_per_run_alias")]

    def __str__(self) -> str:
        return f"run {self.run_id} {self.src_uid}"
