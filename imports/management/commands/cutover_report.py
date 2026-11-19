# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Print the go or no-go report for one organizer. Exit code 1 means no-go, so a pipeline can stop on it."""

import json
import sys

from django.core.management.base import BaseCommand

from imports.cutover import DEFAULT_MAX_OPEN_QUARANTINE_SHARE, cutover_report


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser) -> None:
        parser.add_argument("organizer")
        parser.add_argument("--max-open-share", type=float, default=DEFAULT_MAX_OPEN_QUARANTINE_SHARE)

    def handle(self, *args, **options) -> None:
        report = cutover_report(options["organizer"], options["max_open_share"])
        self.stdout.write(json.dumps(report, indent=2, default=str))
        if not report["go"]:
            sys.exit(1)
