"""Run what CI runs, on Linux, in a clean container, and write docs/test-evidence.json.

Installs from the lock file, then runs lint, types, migration check, tests with coverage, and a dependency audit.
Needs: docker compose up -d db redis. Run with: python scripts/evidence.py
"""

import datetime as dt
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "test-evidence.json"
INSIDE = r"""
set -u
# Code only: the data folder and a local virtual environment would add gigabytes and nothing to test.
mkdir /work && tar -C /src --exclude=./.venv --exclude=./data --exclude=./dist --exclude='./docs/*.webm' --exclude=./.git -cf - . | tar -C /work -xf - && cd /work
pip install -q --root-user-action=ignore -r requirements-dev.lock coverage pip-audit >/dev/null
echo "== python"; python --version; uname -sr
echo "== ruff"; ruff check . && ruff format --check . ; echo "ruff_exit=$?"
echo "== mypy"; mypy access crm imports ticketing mock_api console encore 2>&1 | tail -1; echo "mypy_exit=${PIPESTATUS[0]}"
echo "== migrations"; python manage.py makemigrations --check --dry-run | tail -1; echo "migrations_exit=${PIPESTATUS[0]}"
echo "== tests"; coverage run --source=access,crm,imports,ticketing,encore,console,mock_api --omit='*/migrations/*' -m pytest -q --no-header --junitxml=/tmp/junit.xml -p no:cacheprovider 2>&1 | grep -vE '^\{' | tail -4; echo "tests_exit=${PIPESTATUS[0]}"
echo "== coverage"; coverage report --sort=miss | tail -40
echo "== audit"; pip-audit -r requirements.txt --no-deps --disable-pip --progress-spinner off 2>&1 | tail -12; echo "audit_exit=${PIPESTATUS[0]}"
echo "== junit"; base64 -w0 /tmp/junit.xml; echo
"""


def main() -> None:
    network = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}",
            "encore_mvp-db-1",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    done = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "-v",
            f"{ROOT}:/src:ro",
            "-v",
            "encore-pipcache:/root/.cache/pip",
            "-e",
            "ENCORE_DEBUG=1",
            "-e",
            "DATABASE_URL=postgres://encore:encore@db:5432/encore",
            "-e",
            "REDIS_URL=redis://redis:6379/1",
            "-e",
            "CELERY_BROKER_URL=redis://redis:6379/0",
            "python:3.12-slim",
            "bash",
            "-c",
            INSIDE,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = done.stdout
    if "== junit" not in output:
        print(output[-3000:], done.stderr[-3000:])
        raise SystemExit("The container did not finish")
    head, junit = output.split("== junit")
    import base64

    root = ET.fromstring(base64.b64decode(junit.strip()))
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    cases = []
    for case in suite.iter("testcase"):
        state = "passed"
        if case.find("failure") is not None or case.find("error") is not None:
            state = "failed"
        elif case.find("skipped") is not None:
            state = "skipped"
        cases.append(
            {
                "file": case.get("classname", "").replace(".", "/") + ".py",
                "name": case.get("name"),
                "state": state,
                "seconds": float(case.get("time", 0)),
            }
        )

    def exit_of(name: str) -> int:
        found = re.search(rf"{name}_exit=(\d+)", head)
        return int(found.group(1)) if found else -1

    coverage_rows = re.findall(r"^(\S+\.py)\s+(\d+)\s+(\d+)\s+(\d+)%", head, re.M)
    total = re.search(r"^TOTAL\s+(\d+)\s+(\d+)\s+(\d+)%", head, re.M)
    section = lambda name: head.split(f"== {name}")[1].split("\n== ")[0].strip()  # noqa: E731
    evidence = {
        "measured": dt.date.today().isoformat(),
        "platform": section("python").replace("\n", ", "),
        "tests": {
            "total": len(cases),
            "passed": sum(c["state"] == "passed" for c in cases),
            "failed": sum(c["state"] == "failed" for c in cases),
            "skipped": sum(c["state"] == "skipped" for c in cases),
            "seconds": round(sum(c["seconds"] for c in cases)),
        },
        "checks": {name: exit_of(name) == 0 for name in ("ruff", "mypy", "migrations", "tests")},
        "coverage_percent": int(total.group(3)) if total else None,
        "coverage_least": [
            {"file": f, "percent": int(p)}
            for f, _s, _m, p in sorted(coverage_rows, key=lambda r: int(r[3]))[:8]
        ],
        "audit_clean": exit_of("audit") == 0,
        "audit_output": section("audit").rsplit("audit_exit", 1)[0].strip(),
        "cases": cases,
    }
    OUT.write_text(json.dumps(evidence, indent=1), encoding="utf-8", newline="\n")
    print(json.dumps({k: v for k, v in evidence.items() if k != "cases"}, indent=1))
    if not all(evidence["checks"].values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
