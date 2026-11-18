"""How an erased person is recognised. Separate from crm.erasure so the importer can use it without a cycle."""

import hashlib
from collections.abc import Iterable

from .models import ErasedSubject


def subject_hash(organizer: str, email: str) -> str:
    return hashlib.sha256(f"{organizer}:{email.strip().lower()}".encode()).hexdigest()


def erased_hashes(organizer: str, emails: Iterable[str]) -> set[str]:
    hashes = [subject_hash(organizer, email) for email in emails]
    if not hashes:
        return set()
    return set(
        ErasedSubject.objects.filter(organizer=organizer, email_sha256__in=hashes).values_list(
            "email_sha256", flat=True
        )
    )
