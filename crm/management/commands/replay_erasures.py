"""Apply the erasure ledger again after a restore. --ledger reads the copy kept outside the database, --dump writes it."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from crm.erasure import export_ledger, replay_erasures


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser) -> None:
        parser.add_argument("--ledger")
        parser.add_argument("--dump")

    def handle(self, *args, **options) -> None:
        if options["dump"]:
            Path(options["dump"]).write_text(json.dumps(export_ledger()), encoding="utf-8")
            return
        ledger = json.loads(Path(options["ledger"]).read_text(encoding="utf-8")) if options["ledger"] else None
        self.stdout.write(json.dumps(replay_erasures(ledger)))
