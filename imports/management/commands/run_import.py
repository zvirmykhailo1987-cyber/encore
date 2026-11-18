"""Create and run an import synchronously, or resume one with --resume."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from imports.models import ImportRun
from imports.pipeline import execute_run, reap_stale_runs


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser) -> None:
        parser.add_argument("path", nargs="?")
        parser.add_argument("--resume", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--organizer", default="demo")
        parser.add_argument("--batch-size", type=int)
        parser.add_argument("--reap", action="store_true", help="Release runs of dead workers first")

    def handle(self, *args, **options) -> None:
        if options["reap"]:
            reap_stale_runs(0)
        if options["resume"]:
            run_id = options["resume"]
        else:
            path = Path(options["path"]).resolve()
            if path.name.endswith(".mongoexport.jsonl"):
                suffix = "mongo"
            else:
                suffix = "jsonl" if path.suffix == ".jsonl" else "csv"
            run_id = ImportRun.objects.create(
                organizer=options["organizer"],
                source_path=str(path),
                source_format=suffix,
                dry_run=options["dry_run"],
            ).pk
        self.stdout.write(f"run {run_id}")
        self.stdout.flush()
        run = execute_run(run_id, batch_size=options["batch_size"])
        self.stdout.write(
            json.dumps(
                {
                    "run": run.pk,
                    "status": run.status,
                    "read": run.rows_read,
                    "created": run.rows_created,
                    "updated": run.rows_updated,
                    "merged": run.rows_merged,
                    "skipped": run.rows_skipped,
                    "quarantined": run.rows_quarantined,
                    "error": run.error,
                    "reconciliation": run.reconciliation,
                }
            )
        )
