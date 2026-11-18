"""Independent proof that a run did what its counters say.

The file is read again from the first byte and compared with the database. Nothing here trusts the code
path that wrote the rows.
"""

from itertools import islice
from pathlib import Path

from django.db import connection

from crm.erasure_keys import erased_hashes, subject_hash
from crm.models import Contact, ContactAlias

from .mapping import RowDefect, clean_row
from .models import ImportRun, QuarantineRow
from .source import iter_csv, iter_jsonl, iter_mongoexport, read_header

BATCH = 5000
SAMPLE_LIMIT = 20


def reconcile_run(run: ImportRun) -> dict:
    path = Path(run.source_path)
    quarantined = set(QuarantineRow.objects.filter(run=run).values_list("row_number", flat=True))
    if run.source_format == "jsonl":
        records = iter_jsonl(path, 0, 0)
    elif run.source_format == "mongo":
        records = iter_mongoexport(path, 0, 0)
    else:
        header, offset = read_header(path)
        records = iter_csv(path, header, offset, 0)

    source_rows = 0
    missing: list[str] = []
    missing_total = 0
    unexplained_invalid = 0
    erased_rows = 0
    # A legacy id can appear twice with different content. Only its last appearance must match the database,
    # so a mismatch stays a suspect until a later row for the same id clears it.
    suspects: dict[str, int] = {}

    # Rows the source no longer has: the legacy system deleted them after an earlier export. They are reported rather
    # than removed, because a partial delta file would look the same as mass deletion.
    table = f"reconcile_seen_{run.pk}"
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE TEMP TABLE IF NOT EXISTS {table} (src_uid text)")  # noqa: S608
        cursor.execute(f"TRUNCATE {table}")  # noqa: S608

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
        with connection.connection.cursor() as raw, raw.copy(f"COPY {table} (src_uid) FROM STDIN") as copy:
            for row in clean:
                copy.write_row((row.src_uid,))
        # A person erased after the import is still in the old file. Their rows are expected to be absent.
        emails = {str(row.fields["email"]) for row in clean if row.fields["email"]}
        erased = erased_hashes(run.organizer, emails)
        if erased:
            remaining = [
                row
                for row in clean
                if not (
                    row.fields["email"] and subject_hash(run.organizer, str(row.fields["email"])) in erased
                )
            ]
            erased_rows += len(clean) - len(remaining)
            clean = remaining
        stored = {
            src_uid: (row_hash, email, curated)
            for src_uid, row_hash, email, curated in ContactAlias.objects.filter(
                organizer=run.organizer, src_uid__in=[row.src_uid for row in clean]
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

    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT a.src_uid FROM crm_contactalias a
            WHERE a.organizer = %s AND a.src_uid LIKE %s
              AND NOT EXISTS (SELECT 1 FROM {table} s WHERE s.src_uid = a.src_uid)
            """,  # noqa: S608
            [run.organizer, f"{run.source_system}:%"],
        )
        not_in_source = [row[0] for row in cursor.fetchall()]
        cursor.execute(f"DROP TABLE {table}")  # noqa: S608
    # Rows quarantined in this file were never loaded into the table above, so they do not count as absent.

    aliases_written = ContactAlias.objects.filter(run=run).count()
    contacts_written = Contact.objects.filter(created_by_run=run).count()
    checks = {
        "source_rows_vs_rows_read": source_rows - run.rows_read,
        "counters_vs_rows_read": (
            run.rows_created + run.rows_updated + run.rows_merged + run.rows_skipped + run.rows_quarantined
        )
        - run.rows_read,
        "contacts_written_vs_created": contacts_written + run.contacts_erased - run.rows_created,
        "aliases_written_vs_created_plus_merged": aliases_written
        + run.aliases_erased
        - (run.rows_created + run.rows_merged),
    }
    discrepancies = (
        missing_total + len(suspects) + unexplained_invalid + sum(abs(value) for value in checks.values())
    )
    return {
        "source_rows": source_rows,
        "rows_read": run.rows_read,
        "quarantined_rows": len(quarantined),
        "erased_rows": erased_rows,
        "in_database_not_in_this_file": len(not_in_source),
        "not_in_this_file_sample": not_in_source[:SAMPLE_LIMIT],
        "missing_in_target": missing_total,
        "missing_sample": missing,
        "field_mismatches": len(suspects),
        "mismatch_sample": list(suspects)[:SAMPLE_LIMIT],
        "invalid_rows_not_in_quarantine": unexplained_invalid,
        "checks": checks,
        "discrepancies": discrepancies,
        "balanced": discrepancies == 0,
    }
