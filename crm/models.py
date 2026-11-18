from django.db import models
from django.db.models import Q

#: Used when a run does not name an organizer. Every contact belongs to exactly one organizer.
DEFAULT_ORGANIZER = "demo"

#: Fields the import may write. Everything else on Contact is owned by the new CRM.
IMPORTABLE_FIELDS = (
    "email",
    "phone",
    "first_name",
    "last_name",
    "city",
    "marketing_opt_in",
    "consent_recorded_at",
    "consent_wording",
    "legacy_created_at",
)


class Contact(models.Model):
    """A person in the new CRM. Unknown values stay NULL: unknown is not an empty string or False."""

    #: The customer who owns this record. The same person at two organizers is two separate contacts.
    organizer = models.CharField(max_length=40, default=DEFAULT_ORGANIZER)
    email = models.CharField(max_length=254, null=True, blank=True)
    phone = models.CharField(max_length=32, null=True, blank=True)
    first_name = models.CharField(max_length=120, null=True, blank=True)
    last_name = models.CharField(max_length=120, null=True, blank=True)
    city = models.CharField(max_length=120, null=True, blank=True)
    marketing_opt_in = models.BooleanField(null=True)
    #: Proof of consent is when and what wording, not only yes or no. Unknown stays NULL, and is reported before cutover.
    consent_recorded_at = models.DateField(null=True, blank=True)
    consent_wording = models.CharField(max_length=500, null=True, blank=True)
    legacy_created_at = models.DateField(null=True, blank=True)
    #: Names of fields an operator edited by hand. The import never overwrites these.
    curated_fields = models.JSONField(default=list, blank=True)
    created_by_run = models.ForeignKey(
        "imports.ImportRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="created_contacts"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organizer", "email"],
                condition=Q(email__isnull=False),
                name="contact_email_unique_per_organizer",
            )
        ]

    def __str__(self) -> str:
        return self.email or f"contact-{self.pk}"


class ContactAlias(models.Model):
    """Maps one legacy record to one contact. This table is what makes a re-run idempotent."""

    organizer = models.CharField(max_length=40, default=DEFAULT_ORGANIZER)
    src_uid = models.CharField(max_length=160)
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="aliases")
    row_hash = models.CharField(max_length=64)
    run = models.ForeignKey("imports.ImportRun", null=True, on_delete=models.SET_NULL, related_name="aliases")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organizer", "src_uid"], name="alias_once_per_organizer")
        ]

    def __str__(self) -> str:
        return self.src_uid


class ErasedSubject(models.Model):
    """Someone who asked to be erased. Keeps a later re-import from bringing them back.

    It stores a hash and no readable address. That is still pseudonymous data, so it needs its own retention decision.
    """

    organizer = models.CharField(max_length=40)
    email_sha256 = models.CharField(max_length=64)
    erased_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organizer", "email_sha256"], name="erased_once_per_organizer")
        ]

    def __str__(self) -> str:
        return f"{self.organizer}:{self.email_sha256[:12]}"
