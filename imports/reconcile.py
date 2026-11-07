"""Independent proof that a run did what its counters say.

The file is read again from the first byte and compared with the database. Nothing here trusts the code
path that wrote the rows.
"""

from itertools import islice
from pathlib import Path

from crm.models import Contact, ContactAlias

from .mapping import RowDefect, clean_row
from .models import ImportRun, QuarantineRow
from .source import iter_csv, iter_jsonl, read_header

BATCH = 5000
SAMPLE_LIMIT = 20


def reconcile_run(run: ImportRun) -> dict:
    path = Path(run.source_path)
    quarantined = set(QuarantineRow.objects.filter(run=run).values_list("row_number", flat=True))
    if run.source_format == "jsonl":
        records = iter_jsonl(path, 0, 0)
    else:
        header, offset = read_header(path)
        records = iter_csv(path, header, offset, 0)

    source_rows = 0
    missing: list[str] = []
    missing_total = 0
    unexplained_invalid = 0
    # A legacy id can appear twice with different content. Only its last appearance must match the database,
    # so a mismatch stays a suspect until a later row for the same id clears it.
    suspects: dict[str, int] = {}

    while batch := list(islice(records, BATCH)):
        source_rows += len(batch)
        clean = []
        for record in batch:
            if record.row_number in quarantined:
                continue
            if record.values is None:
                unexplained_invalid += 1
                continue
            try:
                clean.append(clean_row(record.row_number, record.values, run.source_system))
            except RowDefect:
                unexplained_invalid += 1
        stored = {
            src_uid: (row_hash, email, curated)
            for src_uid, row_hash, email, curated in ContactAlias.objects.filter(
                src_uid__in=[row.src_uid for row in clean]
            ).values_list("src_uid", "row_hash", "contact__email", "contact__curated_fields")
        }
        for row in clean:
            found = stored.get(row.src_uid)
            if found is None:
                missing_total += 1
                if len(missing) < SAMPLE_LIMIT:
                    missing.append(row.src_uid)
                continue
            row_hash, stored_email, curated = found
            email = row.fields["email"]
            email_differs = email is not None and stored_email != email and "email" not in curated
            if row_hash != row.row_hash or email_differs:
                suspects[row.src_uid] = row.row_number
            else:
                suspects.pop(row.src_uid, None)

    aliases_written = ContactAlias.objects.filter(run=run).count()
    contacts_written = Contact.objects.filter(created_by_run=run).count()
    checks = {
        "source_rows_vs_rows_read": source_rows - run.rows_read,
        "counters_vs_rows_read": (
            run.rows_created + run.rows_updated + run.rows_merged + run.rows_skipped + run.rows_quarantined
        )
        - run.rows_read,
        "contacts_written_vs_created": contacts_written - run.rows_created,
        "aliases_written_vs_created_plus_merged": aliases_written - (run.rows_created + run.rows_merged),
    }
    discrepancies = (
        missing_total + len(suspects) + unexplained_invalid + sum(abs(value) for value in checks.values())
    )
    return {
        "source_rows": source_rows,
        "rows_read": run.rows_read,
        "quarantined_rows": len(quarantined),
        "missing_in_target": missing_total,
        "missing_sample": missing,
        "field_mismatches": len(suspects),
        "mismatch_sample": list(suspects)[:SAMPLE_LIMIT],
        "invalid_rows_not_in_quarantine": unexplained_invalid,
        "checks": checks,
        "discrepancies": discrepancies,
        "balanced": discrepancies == 0,
    }
