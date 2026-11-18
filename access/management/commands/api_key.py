"""Issue, list and revoke API keys. The plaintext is printed once, at issue time, and never stored."""

import datetime as dt
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from access.models import AccessRecord, ApiKey

ORGANIZER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class Command(BaseCommand):
    help = "Manage API keys and the personal-data access log"

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        issue = sub.add_parser("issue")
        issue.add_argument("--scope", choices=[c.value for c in ApiKey.Scope], required=True)
        issue.add_argument("--organizer")
        issue.add_argument("--name", required=True)
        sub.add_parser("list")
        revoke = sub.add_parser("revoke")
        revoke.add_argument("prefix")
        audit = sub.add_parser("audit")
        audit.add_argument("--limit", type=int, default=50)
        sub.add_parser("purge-audit")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "issue":
            scope, organizer = options["scope"], options.get("organizer")
            if (scope != ApiKey.Scope.PLATFORM) != bool(organizer):
                raise CommandError(
                    "--organizer is required for organizer and gate keys, and refused for platform keys"
                )
            if organizer and not ORGANIZER.match(organizer):
                raise CommandError("An organizer is lower case letters, digits, - and _")
            key, plaintext = ApiKey.issue(options["name"], scope, organizer)
            self.stdout.write(f"Issued {key}. Store it now, it is not shown again:\n{plaintext}")
        elif action == "list":
            for key in ApiKey.objects.order_by("created_at"):
                state = f"revoked {key.revoked_at:%Y-%m-%d}" if key.revoked_at else "active"
                used = f"{key.last_used_at:%Y-%m-%d %H:%M}" if key.last_used_at else "never"
                self.stdout.write(
                    f"{key.prefix}  {key.scope:<9} {key.organizer or '-':<20} {state:<18} used {used}  {key.name}"
                )
        elif action == "audit":
            for record in AccessRecord.objects.all()[: options["limit"]]:
                self.stdout.write(str(record))
        elif action == "purge-audit":
            cutoff = timezone.now() - dt.timedelta(days=settings.ENCORE_AUDIT_RETENTION_DAYS)
            deleted, _ = AccessRecord.objects.filter(at__lt=cutoff).delete()
            self.stdout.write(f"Purged {deleted} access records older than {cutoff:%Y-%m-%d}")
        else:
            matches = list(ApiKey.objects.filter(prefix=options["prefix"], revoked_at__isnull=True))
            if len(matches) != 1:
                raise CommandError(f"{len(matches)} active keys have that prefix")
            matches[0].revoke()
            self.stdout.write(f"Revoked {matches[0]}")
