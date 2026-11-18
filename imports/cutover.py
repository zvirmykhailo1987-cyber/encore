"""Whether one organizer can be switched to the new CRM. Every check is read from the database."""

from crm.models import Contact

from .models import ImportRun, QuarantineRow

DEFAULT_MAX_OPEN_QUARANTINE_SHARE = 0.005


def cutover_report(organizer: str, max_open_share: float = DEFAULT_MAX_OPEN_QUARANTINE_SHARE) -> dict:
    runs = ImportRun.objects.filter(organizer=organizer)
    latest = runs.filter(dry_run=False, status=ImportRun.Status.COMPLETED).order_by("-pk").first()
    unfinished = runs.filter(
        status__in=[ImportRun.Status.RUNNING, ImportRun.Status.INTERRUPTED, ImportRun.Status.PAUSED]
    ).count()
    rows_read = latest.rows_read if latest else 0
    open_rows = QuarantineRow.objects.filter(run=latest, status=QuarantineRow.Status.OPEN).count() if latest else 0
    share = open_rows / rows_read if rows_read else None
    report = latest.reconciliation if latest and latest.reconciliation else {}
    dry_run_first = (
        latest is not None
        and runs.filter(dry_run=True, status=ImportRun.Status.COMPLETED, pk__lt=latest.pk).exists()
    )
    checks = [
        {"name": "A dry run came first", "passed": dry_run_first},
        {"name": "A write import has completed", "passed": latest is not None},
        {"name": "No run is unfinished", "passed": unfinished == 0, "value": unfinished},
        {
            "name": "Reconciliation balanced",
            "passed": bool(report.get("balanced")),
            "value": report.get("discrepancies"),
        },
        {
            "name": f"Open quarantine at most {max_open_share:.1%} of rows",
            "passed": share is not None and share <= max_open_share,
            "value": open_rows,
        },
    ]
    return {
        "organizer": organizer,
        "go": all(check["passed"] for check in checks),
        "checks": checks,
        "contacts": Contact.objects.filter(organizer=organizer).count(),
        # Not a blocker: the organizer decides whether these people may still be emailed.
        "opt_ins_without_proof": Contact.objects.filter(
            organizer=organizer, marketing_opt_in=True, consent_recorded_at__isnull=True
        ).count(),
        "in_database_not_in_source": report.get("in_database_not_in_this_file"),
        "latest_run": latest.pk if latest else None,
        "source_sha256": latest.source_sha256 if latest else None,
        "columns_not_migrated": latest.unmapped_columns if latest else [],
    }
