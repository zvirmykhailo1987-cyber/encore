# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Erase one person from one organizer, and stop later imports from bringing them back."""

import json

from django.core.management.base import BaseCommand

from crm.erasure import erase_subject


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser) -> None:
        parser.add_argument("organizer")
        parser.add_argument("email")

    def handle(self, *args, **options) -> None:
        self.stdout.write(json.dumps(erase_subject(options["organizer"], options["email"])))
