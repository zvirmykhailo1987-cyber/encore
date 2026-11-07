from django.db import models
from django.db.models import Q

#: Fields the import may write. Everything else on Contact is owned by the new CRM.
IMPORTABLE_FIELDS = (
    "email",
    "phone",
    "first_name",
    "last_name",
    "city",
    "marketing_opt_in",
    "legacy_created_at",
)


class Contact(models.Model):
    """A person in the new CRM. Unknown values stay NULL: unknown is not an empty string or False."""

    email = models.CharField(max_length=254, null=True, blank=True)
    phone = models.CharField(max_length=32, null=True, blank=True)
    first_name = models.CharField(max_length=120, null=True, blank=True)
    last_name = models.CharField(max_length=120, null=True, blank=True)
    city = models.CharField(max_length=120, null=True, blank=True)
    marketing_opt_in = models.BooleanField(null=True)
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
                fields=["email"], condition=Q(email__isnull=False), name="contact_email_unique"
            )
        ]

    def __str__(self) -> str:
        return self.email or f"contact-{self.pk}"


class ContactAlias(models.Model):
    """Maps one legacy record to one contact. This table is what makes a re-run idempotent."""

    src_uid = models.CharField(max_length=160, unique=True)
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="aliases")
    row_hash = models.CharField(max_length=64)
    run = models.ForeignKey("imports.ImportRun", null=True, on_delete=models.SET_NULL, related_name="aliases")

    def __str__(self) -> str:
        return self.src_uid
