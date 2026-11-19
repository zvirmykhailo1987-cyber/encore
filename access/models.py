# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""API keys. Each key belongs to one scope; an organizer key sees one organizer's data and nothing else."""

import hashlib
import secrets

from django.db import models
from django.utils import timezone

KEY_PREFIX = "enc"


def digest(plaintext: str) -> str:
    # Keys are 256 random bits, so a fast hash is enough: there is nothing to brute-force.
    return hashlib.sha256(plaintext.encode()).hexdigest()


class ApiKey(models.Model):
    class Scope(models.TextChoices):
        PLATFORM = "platform", "Platform operator"
        ORGANIZER = "organizer", "One organizer"
        GATE = "gate", "Gate device of one organizer"

    name = models.CharField(max_length=80)
    scope = models.CharField(max_length=12, choices=Scope.choices)
    #: Set for organizer and gate keys: whose data the key may touch. NULL only for platform keys.
    organizer = models.CharField(max_length=40, null=True, blank=True)
    #: Shown in lists so a person can tell keys apart. The rest of the key is never stored.
    prefix = models.CharField(max_length=16, db_index=True)
    digest = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(scope__in=["organizer", "gate"], organizer__isnull=False)
                    | models.Q(scope="platform", organizer__isnull=True)
                ),
                name="organizer_set_except_for_platform_keys",
            )
        ]

    def __str__(self) -> str:
        return f"{self.prefix} ({self.scope}{':' + self.organizer if self.organizer else ''})"

    @classmethod
    def issue(cls, name: str, scope: str, organizer: str | None = None) -> tuple["ApiKey", str]:
        """Creates a key and returns the only copy of its plaintext."""
        prefix = secrets.token_hex(4)
        plaintext = f"{KEY_PREFIX}_{prefix}_{secrets.token_urlsafe(32)}"
        key = cls.objects.create(
            name=name, scope=scope, organizer=organizer, prefix=prefix, digest=digest(plaintext)
        )
        return key, plaintext

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])


class AccessRecord(models.Model):
    """Who read or changed rows with personal data, and when. Kept for a fixed period, then purged."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    scope = models.CharField(max_length=12)
    organizer = models.CharField(max_length=40, null=True, blank=True)
    #: The key's prefix, or "bootstrap" for the key from the environment.
    key_prefix = models.CharField(max_length=16)
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=200)
    status = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["-at"]

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M:%S} {self.key_prefix} {self.method} {self.path} {self.status}"
