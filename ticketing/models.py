from django.db import models


class Ticket(models.Model):
    external_id = models.CharField(max_length=64, unique=True)
    event_id_external = models.CharField(max_length=64, db_index=True)
    holder_email = models.CharField(max_length=254, null=True, blank=True)
    status = models.CharField(max_length=20, default="valid")
    #: Upstream version. Older versions arriving late are ignored.
    version = models.IntegerField(default=1)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_in_scan_id = models.CharField(max_length=64, null=True, blank=True)

    def __str__(self) -> str:
        return self.external_id


class SyncCursor(models.Model):
    name = models.CharField(max_length=40, unique=True)
    cursor = models.CharField(max_length=100, blank=True)
    pages_synced = models.BigIntegerField(default=0)
    items_applied = models.BigIntegerField(default=0)
    items_stale = models.BigIntegerField(default=0)
    last_success_at = models.DateTimeField(null=True, blank=True)
    caught_up = models.BooleanField(default=False)
    #: Position of the newest item, as last reported by the API. NULL until it has said so.
    feed_head = models.BigIntegerField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.name}@{self.cursor}"


class DeadLetter(models.Model):
    """A message that needs a person. It is kept in full and never silently dropped."""

    kind = models.CharField(max_length=30)
    dedupe_key = models.CharField(max_length=64, unique=True)
    payload = models.JSONField()
    reason = models.CharField(max_length=300)
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.kind}: {self.reason}"


class WebhookEvent(models.Model):
    event_id = models.CharField(max_length=64, unique=True)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.event_id


class OutboundCheckin(models.Model):
    """Transactional outbox: written with the scan, delivered later, at least once, with a stable key."""

    class Status(models.TextChoices):
        PENDING = "pending"
        SENT = "sent"
        DEAD = "dead"

    ticket_external_id = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.IntegerField(default=0)
    next_attempt_at = models.DateTimeField(auto_now_add=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="outbox_due")]

    def __str__(self) -> str:
        return f"{self.idempotency_key} {self.status}"


class ScanConflict(models.Model):
    """Two gates admitted one ticket while offline, or a gate admitted a ticket the server has never seen."""

    scan_id = models.CharField(max_length=64, unique=True)
    ticket_external_id = models.CharField(max_length=64, db_index=True)
    device = models.CharField(max_length=64)
    scanned_at = models.DateTimeField()
    reason = models.CharField(max_length=20)
    first_scan_id = models.CharField(max_length=64, blank=True)
    reviewed = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.ticket_external_id}: {self.reason}"


class ConnectorMetric(models.Model):
    """One row per minute. Latency uses fixed buckets so workers can merge their numbers."""

    minute = models.DateTimeField(unique=True)
    requests = models.BigIntegerField(default=0)
    successes = models.BigIntegerField(default=0)
    retries = models.BigIntegerField(default=0)
    rate_limited = models.BigIntegerField(default=0)
    server_errors = models.BigIntegerField(default=0)
    timeouts = models.BigIntegerField(default=0)
    client_errors = models.BigIntegerField(default=0)
    breaker_rejections = models.BigIntegerField(default=0)
    latency_buckets = models.JSONField(default=dict)

    class Meta:
        ordering = ["-minute"]

    def __str__(self) -> str:
        return str(self.minute)


class ConnectorState(models.Model):
    """Circuit breaker state shared by all workers."""

    name = models.CharField(max_length=40, unique=True)
    state = models.CharField(max_length=12, default="closed")
    consecutive_failures = models.IntegerField(default=0)
    opened_at = models.FloatField(null=True, blank=True)
    times_opened = models.IntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.name}: {self.state}"
