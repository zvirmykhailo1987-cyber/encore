"""Break the code on purpose and check that the tests notice. Writes docs/mutation-results.json.

Coverage says a line ran. This says a test fails when the line is wrong. Each mutation removes one guarantee from a
temporary copy of the project, and the whole suite runs against that copy. A mutation nothing catches is reported as
"survived", not hidden. Needs PostgreSQL and Redis from: docker compose up -d db redis
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
SKIP = {".venv", "data", ".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "shots", "design", "dist"}

MUTATIONS = [
    ("One organizer's contacts merge with another's", "imports/pipeline.py",
     "Contact.objects.filter(organizer=organizer, email__in=emails)", "Contact.objects.filter(email__in=emails)"),
    ("A re-run ignores which organizer a legacy id belongs to", "imports/pipeline.py",
     "organizer=organizer, src_uid__in=src_uids", "src_uid__in=src_uids"),
    ("The checkpoint does not move with the batch", "imports/pipeline.py",
     "            checkpoint_offset=last.end_offset,\n", ""),
    ("A stale worker keeps writing after losing its lease", "imports/pipeline.py",
     "if current.lease_token != token or current.status != ImportRun.Status.RUNNING:", "if False:"),
    ("The import overwrites a value a person corrected", "imports/pipeline.py",
     "if value is not None and name not in contact.curated_fields:", "if value is not None:"),
    ("A blank in the source erases a known value", "imports/pipeline.py",
     "if value is not None and name not in contact.curated_fields:", "if name not in contact.curated_fields:"),
    ("An erased person comes back on the next run", "imports/pipeline.py",
     "    if erased:\n        kept = []", "    if False:\n        kept = []"),
    ("Rollback undoes a run that later runs built on", "imports/pipeline.py",
     "        if later.exists():\n", "        if False:\n"),
    ("A resume accepts a changed source file", "imports/pipeline.py",
     "if run.source_sha256 and run.source_sha256 != digest:", "if False:"),
    ("Reconciliation always reports balanced", "imports/reconcile.py",
     '"balanced": discrepancies == 0,', '"balanced": True,'),
    ("Reconciliation ignores rows missing from the database", "imports/reconcile.py",
     "                missing_total += 1\n", "                pass\n"),
    ("Unknown consent becomes a refusal", "imports/mapping.py",
     '    value = value.strip().lower()\n    if not value:\n        return None\n    if value in _TRUE:',
     '    value = value.strip().lower()\n    if not value:\n        return False\n    if value in _TRUE:'),
    ("A broken email is accepted", "imports/mapping.py",
     'raise RowDefect("invalid_email", value)', "return value"),
    ("A late event overwrites a newer version", "ticketing/services.py",
     "            WHERE ticketing_ticket.version < EXCLUDED.version\n", ""),
    ("Two gates can admit one ticket", "ticketing/services.py",
     'external_id=ticket_external_id, checked_in_at__isnull=True, status="valid"\n            ).update(checked_in_at=now',
     'external_id=ticket_external_id, status="valid"\n            ).update(checked_in_at=now'),
    ("A cancelled ticket is admitted", "ticketing/services.py",
     'external_id=ticket_external_id, checked_in_at__isnull=True, status="valid"\n            ).update(checked_in_at=now',
     'external_id=ticket_external_id, checked_in_at__isnull=True\n            ).update(checked_in_at=now'),
    ("A check-in is admitted without its outbox message", "ticketing/services.py",
     "                OutboundCheckin.objects.create(ticket_external_id=ticket_external_id, idempotency_key=scan_id)\n"
     '                return ScanResult("admitted", 200, now)',
     '                return ScanResult("admitted", 200, now)'),
    ("A duplicate webhook is applied again", "ticketing/services.py",
     '        except IntegrityError:\n            return "duplicate"', "        except IntegrityError:\n            pass"),
    ("The outbox lease is gone, so workers double-send", "ticketing/services.py",
     "        message.next_attempt_at = timezone.now() + dt.timedelta(seconds=OUTBOX_LEASE_SECONDS)\n", ""),
    ("A second offline gate is not reported", "ticketing/services.py",
     'reason = "already_used" if ticket.checked_in_at is not None else f"ticket_{ticket.status}"', 'reason = "accepted"'),
    ("A card number pasted into a notes field is imported", "imports/mapping.py",
     "    if _contains_card_number(values):\n", "    if False:\n"),
    ("A refunded ticket scanned offline counts as a normal entry", "ticketing/services.py",
     'external_id=ticket_external_id, checked_in_at__isnull=True, status="valid"\n            ).update(checked_in_at=scanned_at',
     'external_id=ticket_external_id, checked_in_at__isnull=True\n            ).update(checked_in_at=scanned_at'),
    ("A forged webhook is accepted", "ticketing/security.py",
     "return hmac.compare_digest(sign_webhook(secret, timestamp, body), signature)", "return True"),
    ("An old webhook can be replayed", "ticketing/security.py",
     "if age > WEBHOOK_TOLERANCE_SECONDS:", "if False:"),
    ("Anyone can read quarantined personal data", "ticketing/security.py",
     "        if not settings.ENCORE_OPERATOR_TOKEN:\n            return bool(settings.DEBUG)\n        return _token_supplied(request)",
     "        return True"),
    ("A 429 opens the circuit breaker", "ticketing/client.py",
     '                    self.stats.counts["rate_limited"] += 1\n                    self.breaker.record_success()',
     '                    self.stats.counts["rate_limited"] += 1\n                    self.breaker.record_failure()'),
    ("Retry-After is ignored", "ticketing/client.py",
     "retry_after = _retry_after(response)", "retry_after = None"),
    ("A permanent 4xx error is retried", "ticketing/client.py",
     "                    raise PermanentError(response.status_code, response.text)\n", ""),
    ("A check-in can be sent with no idempotency key", "ticketing/client.py",
     "        if not idempotency_key:\n", "        if False:\n"),
    ("Long imports share workers with ticket sync", "encore/settings.py",
     'CELERY_TASK_ROUTES = {"imports.run_import": {"queue": "imports"}, "imports.reconcile": {"queue": "imports"}}',
     "CELERY_TASK_ROUTES = {}"),
]


def copy_project(target: Path) -> None:
    shutil.copytree(ROOT, target, ignore=lambda _d, names: [n for n in names if n in SKIP], dirs_exist_ok=True)


def run_tests(cwd: Path) -> tuple[bool, str]:
    env = {**os.environ, "ENCORE_DEBUG": "1", "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run(
        [PYTHON, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider", "--create-db"],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    found = re.findall(r"^FAILED (\S+)", done.stdout, re.M) or re.findall(r"^ERROR (\S+)", done.stdout, re.M)
    return done.returncode == 0, found[0] if found else ""


def main() -> None:
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "copy"
        copy_project(work)
        passed, _ = run_tests(work)
        if not passed:
            raise SystemExit("The unmodified copy does not pass, so nothing here could be trusted.")
        for name, relative, old, new in MUTATIONS:
            path = work / relative
            original = path.read_text(encoding="utf-8")
            if old not in original:
                raise SystemExit(f"Mutation no longer applies, fix the script: {name}")
            path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
            passed, caught_by = run_tests(work)
            path.write_text(original, encoding="utf-8", newline="\n")
            results.append(
                {"bug": name, "file": relative, "caught": not passed, "caught_by": caught_by if not passed else None}
            )
            print(("caught  " if not passed else "SURVIVED"), name, caught_by, flush=True)
    summary = {
        "measured": dt.date.today().isoformat(),
        "bugs_injected": len(results),
        "caught": sum(row["caught"] for row in results),
        "results": results,
    }
    (ROOT / "docs" / "mutation-results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8", newline="\n")
    print(summary["caught"], "of", summary["bugs_injected"])


if __name__ == "__main__":
    main()
