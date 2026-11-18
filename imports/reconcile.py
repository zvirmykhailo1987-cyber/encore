"""Independent proof that a run did what its counters say.

The file is read again from the first byte and compared with the database. Fields are compared with what
the contact holds now, not with a hash the import wrote, so a field the import failed to save is caught.
"""

import json
from itertools import islice
from pathlib import Path

from django.db import connection
from django.db.models import Count

from crm.erasure_keys import erased_hashes, subject_hash
from crm.models import IMPORTABLE_FIELDS, Contact, ContactAlias

from .mapping import RowDefect, clean_row
from .models import ImportRun, QuarantineRow
from .source import iter_csv, iter_jsonl, iter_mongoexport, read_header

BATCH = 5000
SAMPLE_LIMIT = 20


def _same(stored: object, expected: object) -> bool:
    return json.dumps(stored, default=str) == json.dumps(expected, default=str)


def fields_differ(row_fields: dict, contact: dict, shared: bool) -> bool:
    """Whether the contact does not hold what this row says.

    A contact that two legacy ids merged into keeps the first value of each field, so only the email is
    compared there. Fields a person curated in the new CRM are theirs, not the import's.
    """
    curated = set(contact["curated_fields"] or [])
    for name, value in row_fields.items():
        if value is None or name in curated:
            continue
        if shared and name != "email":
            continue
        if not _same(contact[name], value):
            return True
    return False


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
        stored = dict(
            ContactAlias.objects.filter(
                organizer=run.organizer, src_uid__in=[row.src_uid for row in clean]
            ).values_list("src_uid", "contact_id")
        )
        contact_ids = set(stored.values())
        contacts = {
            values["pk"]: values
            for values in Contact.objects.filter(pk__in=contact_ids).values(
                "pk", *IMPORTABLE_FIELDS, "curated_fields"
            )
        }
        alias_counts = dict(
            ContactAlias.objects.filter(contact_id__in=contact_ids)
            .values("contact_id")
            .annotate(n=Count("pk"))
            .values_list("contact_id", "n")
        )
        for row in clean:
            contact_id = stored.get(row.src_uid)
            if contact_id is None or contact_id not in contacts:
                missing_total += 1
                if len(missing) < SAMPLE_LIMIT:
                    missing.append(row.src_uid)
                continue
            if fields_differ(row.fields, contacts[contact_id], alias_counts.get(contact_id, 0) > 1):
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
