# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir

"""Profile one import on a scratch database. Usage: python scripts/profile_import.py <rows>"""

import cProfile
import os
import pstats
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ENCORE_DEBUG", "1")
os.environ.setdefault("DATABASE_URL", "postgres://encore:encore@127.0.0.1:5433/encore_bench")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "encore.settings")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402

from crm.models import Contact  # noqa: E402
from imports.management.commands.gen_legacy import generate  # noqa: E402
from imports.models import ImportRun  # noqa: E402
from imports.pipeline import execute_run  # noqa: E402

rows = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
call_command("migrate", verbosity=0)
Contact.objects.all().delete()
ImportRun.objects.all().delete()

with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "bench.csv"
    generate(path, rows)
    run = ImportRun.objects.create(source_path=str(path))
    profiler = cProfile.Profile()
    started = time.perf_counter()
    profiler.enable()
    result = execute_run(run.pk)
    profiler.disable()
    elapsed = time.perf_counter() - started

print(f"{rows} rows, {elapsed:.1f}s with the profiler on, status {result.status}")
print("balanced:", result.reconciliation["balanced"])
stats = pstats.Stats(profiler)
stats.sort_stats("cumulative").print_stats(28)
