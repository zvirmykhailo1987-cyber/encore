# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Record which organizer an event belongs to. Gate keys of that organizer then see its tickets."""

import re

from django.core.management.base import BaseCommand, CommandError

from ticketing.models import EventOwner

ORGANIZER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class Command(BaseCommand):
    help = "Assign events to organizers, or list the assignments"

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        assign = sub.add_parser("assign")
        assign.add_argument("organizer")
        assign.add_argument("events", nargs="+")
        sub.add_parser("list")

    def handle(self, *args, **options):
        if options["action"] == "list":
            for owner in EventOwner.objects.order_by("organizer", "event_external_id"):
                self.stdout.write(str(owner))
            return
        organizer = options["organizer"]
        if not ORGANIZER.match(organizer):
            raise CommandError("An organizer is lower case letters, digits, - and _")
        for event in options["events"]:
            if not 0 < len(event) <= 64:
                raise CommandError(f"Event id {event!r} must be 1 to 64 characters")
            owner, created = EventOwner.objects.get_or_create(
                event_external_id=event, defaults={"organizer": organizer}
            )
            if not created and owner.organizer != organizer:
                # Moving an event between organizers would hand its gate data to someone else. Done on purpose only.
                raise CommandError(f"{event} already belongs to {owner.organizer}")
            self.stdout.write(f"{event} -> {organizer}")
