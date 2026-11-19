# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Erasure requests that arrive while a migration is still running.

A person can ask to be erased after the export was taken. Deleting the contact is not enough: the next re-run of
that export would create them again. So the request leaves a hash behind, and the import refuses matching rows.
"""

import json
import re

from django.db import transaction
from django.db.models import F, Q, TextField
from django.db.models.functions import Cast

from imports.models import ContactChange, ImportRun, QuarantineRow
from ticketing.models import DeadLetter, EventOwner, Ticket, WebhookEvent

from .erasure_keys import subject_hash
from .models import Contact, ContactAlias, ErasedSubject


def erase_subject(organizer: str, email: str) -> dict:
    """Remove one person from one organizer. Other organizers are separate records and stay untouched."""
    from imports.pipeline import lock_organizer

    address = email.strip().lower()
    with transaction.atomic():
        # A running import of this organizer finishes its batch first, then sees the ledger entry.
        lock_organizer(organizer)
        ErasedSubject.objects.get_or_create(
            organizer=organizer, email_sha256=subject_hash(organizer, address)
        )
        contacts = list(Contact.objects.select_for_update().filter(organizer=organizer, email=address))
        aliases_removed = 0
        for contact in contacts:
            # Counters stay as they were, so reconciliation is told what disappeared and why.
            for alias in ContactAlias.objects.filter(contact=contact, run__isnull=False):
                ImportRun.objects.filter(aliases=alias).update(aliases_erased=F("aliases_erased") + 1)
                aliases_removed += 1
            if contact.created_by_run_id:
                ImportRun.objects.filter(pk=contact.created_by_run_id).update(
                    contacts_erased=F("contacts_erased") + 1
                )
            contact.delete()
        # Broken rows come in several shapes (named columns, a list of fields, a raw line), so match the text.
        quarantined = (
            QuarantineRow.objects.filter(run__organizer=organizer)
            .annotate(raw_text=Cast("raw", TextField()))
            .filter(Q(raw_text__icontains=address) | Q(detail__icontains=address))
        )
        quarantine_removed = QuarantineRow.objects.filter(pk__in=quarantined.values("pk")).update(
            raw={}, detail="", status=QuarantineRow.Status.DISMISSED
        )
        # Earlier states kept for rollback. Without this, undoing a later run could bring the person back.
        snapshots_removed, _ = ContactChange.objects.filter(
            run__organizer=organizer, before__email__iexact=address
        ).delete()
        ticketing = _erase_from_ticketing(organizer, address)
    return {
        "contacts_removed": len(contacts),
        "aliases_removed": aliases_removed,
        "quarantine_rows_cleared": quarantine_removed,
        "rollback_snapshots_removed": snapshots_removed,
        **ticketing,
    }


def _redact(value: object, address: str) -> object:
    pattern = re.compile(re.escape(address), re.IGNORECASE)
    return json.loads(pattern.sub("[erased]", json.dumps(value)))


def _erase_from_ticketing(organizer: str, address: str) -> dict:
    """Ticket holder emails and stored feed messages of this organizer's events.

    The ticketing provider keeps its own copy, which only the provider can erase. Messages of events with no
    recorded owner cannot be attributed to an organizer and are left alone; they expire with the retention job.
    """
    events = set(EventOwner.objects.filter(organizer=organizer).values_list("event_external_id", flat=True))
    holders = Ticket.objects.filter(event_id_external__in=events, holder_email__iexact=address).update(
        holder_email=None
    )
    redacted = 0
    for model in (WebhookEvent, DeadLetter):
        rows = model.objects.annotate(text=Cast("payload", TextField())).filter(text__icontains=address)
        for row in rows:
            if any(f'"{event}"' in row.text for event in events):
                model.objects.filter(pk=row.pk).update(payload=_redact(row.payload, address))
                redacted += 1
    return {"ticket_holders_cleared": holders, "ticketing_messages_redacted": redacted}


def replay_erasures(ledger: list[dict] | None = None) -> dict:
    """After restoring a backup, erased people can be back. Applies the ledger again.

    A restore also rolls back the ledger table, so production keeps a copy outside the database and passes it in here.
    The ledger holds hashes, so every contact of the affected organizers has to be hashed and compared.
    """
    if ledger:
        for entry in ledger:
            ErasedSubject.objects.get_or_create(
                organizer=entry["organizer"], email_sha256=entry["email_sha256"]
            )
    removed = 0
    for organizer in ErasedSubject.objects.values_list("organizer", flat=True).distinct():
        hashes = set(ErasedSubject.objects.filter(organizer=organizer).values_list("email_sha256", flat=True))
        contacts = Contact.objects.filter(organizer=organizer, email__isnull=False).only("pk", "email")
        for contact in contacts.iterator(chunk_size=5000):
            if subject_hash(organizer, contact.email or "") in hashes:
                erase_subject(organizer, contact.email or "")
                removed += 1
    return {"contacts_removed": removed, "ledger_entries": ErasedSubject.objects.count()}


def export_ledger() -> list[dict]:
    rows = ErasedSubject.objects.order_by("pk").values_list("organizer", "email_sha256")
    return [{"organizer": organizer, "email_sha256": digest} for organizer, digest in rows]
