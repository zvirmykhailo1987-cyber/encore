"""Erasure requests that arrive while a migration is still running.

A person can ask to be erased after the export was taken. Deleting the contact is not enough: the next re-run of
that export would create them again. So the request leaves a hash behind, and the import refuses matching rows.
"""

from django.db import transaction
from django.db.models import F

from imports.models import ImportRun, QuarantineRow

from .erasure_keys import subject_hash
from .models import Contact, ContactAlias, ErasedSubject


def erase_subject(organizer: str, email: str) -> dict:
    """Remove one person from one organizer. Other organizers are separate records and stay untouched."""
    address = email.strip().lower()
    with transaction.atomic():
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
        quarantined = QuarantineRow.objects.filter(run__organizer=organizer, raw__EMAIL_ADDR__iexact=address)
        quarantine_removed = quarantined.update(raw={}, detail="", status=QuarantineRow.Status.DISMISSED)
    return {
        "contacts_removed": len(contacts),
        "aliases_removed": aliases_removed,
        "quarantine_rows_cleared": quarantine_removed,
    }


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
