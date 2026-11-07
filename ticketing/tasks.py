from dataclasses import asdict

from celery import shared_task

from .resilience import RetryPolicy
from .services import flush_outbox, run_with_client, sync_tickets


@shared_task(name="ticketing.sync_tickets")
def sync_task() -> dict:
    return asdict(run_with_client(sync_tickets))


@shared_task(name="ticketing.flush_outbox")
def flush_task() -> dict:
    return asdict(run_with_client(flush_outbox, policy=RetryPolicy(max_attempts=1)))
