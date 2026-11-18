import datetime as dt
from dataclasses import asdict

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import WebhookEvent
from .resilience import RetryPolicy
from .services import flush_outbox, run_with_client, sync_tickets


@shared_task(name="ticketing.sync_tickets")
def sync_task() -> dict:
    return asdict(run_with_client(sync_tickets))


@shared_task(name="ticketing.flush_outbox")
def flush_task() -> dict:
    return asdict(run_with_client(flush_outbox, policy=RetryPolicy(max_attempts=1)))


@shared_task(name="ticketing.purge_webhook_events")
def purge_webhook_events() -> int:
    """Webhook payloads hold holder emails. They are needed only while a provider may still redeliver."""
    cutoff = timezone.now() - dt.timedelta(days=settings.TICKETING_WEBHOOK_RETENTION_DAYS)
    deleted, _ = WebhookEvent.objects.filter(received_at__lt=cutoff).delete()
    return deleted
